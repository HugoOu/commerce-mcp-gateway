"""证据层（ADR-0003）——**本模块由你手写**（`AGENTS.md` 分工表）。

W2 的 `EvidenceLog`（JSONL 一行一条）留在这里当**临时实现**，保证网关在 W3 期间仍能跑；
W3 你要写的是 `ChainLedger`：全局哈希链 + 运行证据包（JSONL + manifest + Merkle 根）
+ `verify` + `replay`。

语义约定（写实现时必须满足）：

1. `record_pending` 必须在**转发之前**落盘（ADR-0022 的「证据先于副作用」）。
2. `record_done` 记上游摘要、脱敏标记与最终预算状态。
3. 每条记录含 `prev_hash` 与自己的 `entry_hash`；规范化用键排序（ADR-0014）。
4. **重放前置条件的最小字段集**（`docs/evaluation.md`）：规范化输入、`policy_digest`、
   预算前后、上游摘要——缺一个，I4 的「可重构」就只是口号。
5. `verify` 要能检出篡改**与截断**（截断靠运行证据包的 Merkle 根 / 锚点）。
6. deny 也要留痕（威胁模型 T9）。
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Literal, Protocol

from ..hashing import digest, sha256_hex

__all__ = [
    "ChainLedger",
    "EvidenceEntry",
    "EvidenceLedger",
    "EvidenceLog",
    "ReconstructReport",
    "ReplayReport",
    "VerifyReport",
    "digest",
]

GENESIS = "genesis"
"""链首的 `prev_hash`：一个不可能与真实哈希混淆的常量。"""
SCHEMA_VERSION = 2
"""证据条目的 schema 版本。

v2（2026-10-16）：加了 `suspected_adversarial`（威胁模型 T2 要求标记疑似对抗内容）。

**字段增加会让旧链不可验**——`_entry_hash` 覆盖全部字段，旧行缺这个键时会被补上默认值，
重算出来的哈希与存的不符。这不是缺陷，而是哈希链的固有性质，也正是这个常量的用途：
本版只保证**同版本**链可验；换版本时重新生成证据（`runs/` 本来就在 `.gitignore` 里）。
"""

REPLAY_REQUIRED: dict[str, tuple[str, ...]] = {
    "pending": ("run_id", "seq", "session_id", "method", "decision", "args_digest", "policy_digest"),
    "done": ("run_id", "seq", "session_id", "method", "decision", "result_digest"),
}
"""重放前置条件的最小字段集（`docs/evaluation.md` 的 I4 口径）。缺哪个字段，那条就没法重放。"""

RECONSTRUCTABLE: frozenset[str] = frozenset({"tools/call", "resources/read"})
"""参与**重构**的方法（ADR-0028）：它们的决策来自策略，所以能被重算。
`tools/list` 的 `allow` 是执行点自己写的，不属于可复算的范畴。"""


def _decision_rows(rows: Sequence[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """每次调用取**第一条**记录——决策写在它上面。

    为什么不只看 `phase == "pending"`：`resources/read` 被允许时只写一条 `done`（它随后必然报
    「不存在」），只看 pending 会把这条决策整个漏掉。第一次出现的行在所有路径上都带着决策。
    """
    first: dict[int, dict[str, Any]] = {}
    for row in rows:
        first.setdefault(int(row["seq"]), row)
    return first


@dataclass(frozen=True)
class EvidenceEntry:
    """一次被拦截调用的记录（ADR-0003 的链上单元）。"""

    phase: Literal["pending", "done"]
    seq: int
    run_id: str
    ts: float
    session_id: str
    method: str
    decision: str
    tool_call_id: str | None = None
    request_id: str | None = None
    tool: str | None = None
    upstream: str | None = None
    reason_code: str | None = None
    outcome: str | None = None
    sanitized: bool = False
    """这次结果是否被净化过（ADR-0003 的 record_done 要记脱敏标记）。"""
    suspected_adversarial: bool = False
    """结果里是否**疑似**有对抗内容（威胁模型 T2 的标记）。

    与 `sanitized` 刻意分开：`sanitized` 说的是「内容被改过」（PII 脱敏 / 丢块），
    这一条说的是「内容没被改，但看起来像注入」。两者都**只标记、不改判定**——
    净化不改变决策（I1）。
    """
    args_digest: str | None = None
    result_digest: str | None = None
    policy_digest: str | None = None
    budget_before: float | None = None
    budget_after: float | None = None
    prev_hash: str | None = None
    entry_hash: str | None = None


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    entries: int
    broken_at: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class ReplayReport:
    ok: bool
    decisions: tuple[str, ...] = ()
    mismatches: tuple[int, ...] = ()
    preconditions_met: bool = False


@dataclass(frozen=True)
class ReconstructReport:
    """重放**重构**的结果——两个数分开报（ADR-0028）。"""

    eligible: int
    """参与重构的条目数（只有 `tools/call` 与 `resources/read`：它们的决策来自策略）。"""
    preconditions_met: int
    """其中「输入件在、且摘要对得上」的条数。"""
    matched: int | None = None
    """其中「用真策略重算的决策与链上一致」的条数；没传评估器就是 `None`。"""
    mismatches: tuple[int, ...] = ()
    skipped: tuple[tuple[int, str], ...] = ()
    """被跳过的条目与原因——**不隐藏缺口**，它是结论的一部分。"""
    detail: str = ""

    @property
    def precondition_rate(self) -> float:
        """前置条件满足率：证据够不够用来重构。"""
        return self.preconditions_met / self.eligible if self.eligible else 0.0

    @property
    def reconstruction_rate(self) -> float | None:
        """决策重构率：重构出来对不对。"""
        if self.matched is None:
            return None
        return self.matched / self.eligible if self.eligible else 0.0


class EvidenceLedger(Protocol):
    """执行点只依赖这几个方法。"""

    def record_pending(self, entry: EvidenceEntry) -> EvidenceEntry: ...

    def record_done(self, entry: EvidenceEntry) -> EvidenceEntry: ...

    def record_inputs(self, run_id: str, seq: int, arguments: dict[str, Any]) -> pathlib.Path: ...

    def verify(self, run_id: str | None = None) -> VerifyReport: ...

    def replay(
        self, run_id: str | None = None, *, evaluator: Callable[[EvidenceEntry], str] | None = None
    ) -> ReplayReport: ...

    def reconstruct(
        self,
        run_id: str | None = None,
        *,
        evaluator: Callable[[EvidenceEntry, dict[str, Any]], str] | None = None,
    ) -> ReconstructReport: ...

    def package(self, run_id: str, target: pathlib.Path, *, with_inputs: bool = False) -> pathlib.Path: ...


def _entry_from_row(row: dict[str, Any]) -> EvidenceEntry:
    """把链上的一行还原成 `EvidenceEntry`（只取数据类认识的字段）。"""
    known = {field.name for field in fields(EvidenceEntry)}
    return EvidenceEntry(**{key: value for key, value in row.items() if key in known})


def _entry_hash(entry: EvidenceEntry) -> str:
    """条目哈希：除 `entry_hash` 外的**全部字段**（含 `prev_hash`）参与计算。

    含 `prev_hash` 是关键：改了任何一条，它后面所有条目的哈希都会变，链因此不可局部伪造。
    """
    payload = asdict(entry)
    payload.pop("entry_hash", None)
    return sha256_hex(payload)


def _merkle_root(hashes: Sequence[str]) -> str:
    """运行证据包的 Merkle 根。落单的节点直接晋级（不复制，免得与「重复叶」混淆）。"""
    if not hashes:
        return sha256_hex({"entries": 0})
    level = list(hashes)
    while len(level) > 1:
        promoted = [sha256_hex([level[i], level[i + 1]]) for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            promoted.append(level[-1])
        level = promoted
    return level[0]


def _has_preconditions(row: dict[str, Any]) -> bool:
    return all(row.get(field) is not None for field in REPLAY_REQUIRED[row["phase"]])


def _load_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _verify_rows(
    rows: Sequence[dict[str, Any]], *, anchors: set[str] | None = None
) -> tuple[bool, int | None, str]:
    """链自洽检查：`prev_hash` 接得上，且每条 `entry_hash` 重算一致。"""
    previous = GENESIS
    for index, row in enumerate(rows):
        # 校验**单个运行包**时，它的第一条接的是链上另一条记录（多运行共用一个证据目录，
        # 见 `_append`）。所以允许首条锚定到链上真实存在的某个 entry_hash。
        anchored = index == 0 and bool(anchors) and row.get("prev_hash") in (anchors or set())
        if row.get("prev_hash") != previous and not anchored:
            return False, index, f"第 {index} 条的 prev_hash 接不上前一条"
        try:
            recomputed = _entry_hash(_entry_from_row(row))
        except TypeError as exc:
            return False, index, f"第 {index} 条字段缺失，无法重算：{exc}"
        if recomputed != row.get("entry_hash"):
            return False, index, f"第 {index} 条的 entry_hash 重算不符（被改过？）"
        previous = str(row.get("entry_hash"))
    return True, None, "链自洽"


class ChainLedger:
    """全局哈希链 + 运行证据包（ADR-0003）。

    目录结构（`path` 是目录）：

    - `chain.jsonl`：全局链，每次被拦截的调用一条（**含 deny**），带 `prev_hash` / `entry_hash`；
    - `runs/<run_id>.jsonl`：该次运行的记录（与链上同一条目）；
    - `runs/<run_id>.manifest.json`：封印——条数、Merkle 根、全部叶哈希、首末哈希。

    能检出什么、检不出什么（诚实边界）：

    - **能**：单条篡改（哈希重算不符）、包内截断（manifest 记了条数与根）、**链被截断**
      （各运行包的叶哈希必须都能在链上找到——链与包互为见证）；
    - **不能**：把链与全部 manifest 一起重写的对手。那需要外部锚点或见证（威胁模型 R5，本版未采纳），
      所以「verify 通过」的含义是**与本地记录一致**，不是「绝对没被动过」。

    写入顺序：**先写链、再写包、最后更新 manifest**。中途崩溃会让包比链少一条，`verify` 会报出来——
    宁可报出来，也不要静默地让账本和证据对不上。
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.runs_dir = path / "runs"
        self.chain_path = path / "chain.jsonl"
        path.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.chain_path.touch()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 写入

    def record_pending(self, entry: EvidenceEntry) -> EvidenceEntry:
        """转发**之前**落盘（ADR-0022 的「证据先于副作用」）。"""
        return self._append(replace(entry, phase="pending"))

    def record_done(self, entry: EvidenceEntry) -> EvidenceEntry:
        """转发**之后**落盘：上游摘要、脱敏标记与最终预算状态。"""
        return self._append(replace(entry, phase="done"))

    def _append(self, entry: EvidenceEntry) -> EvidenceEntry:
        with self._lock:
            rows = _load_rows(self._run_path(entry.run_id))
            for row in rows:
                if row.get("seq") == entry.seq and row.get("phase") == entry.phase:
                    raise ValueError(f"同一 (run, seq, phase) 不能重复记录：{entry.run_id}#{entry.seq}/{entry.phase}")

            # prev_hash 必须来自**全局链**的最后一条，不是本运行文件的最后一条。
            # 取错来源时，一个证据目录里跑第二次运行，它的第一条就会接在 genesis 上——
            # 单运行的进程看不出来，S0（25 次运行共用一个证据目录）当场断成 broken_at=11。
            chain = _load_rows(self.chain_path)
            previous = chain[-1]["entry_hash"] if chain else GENESIS
            stored = replace(entry, prev_hash=previous)
            stored = replace(stored, entry_hash=_entry_hash(stored))
            line = json.dumps(asdict(stored), ensure_ascii=False) + "\n"
            with self.chain_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            with self._run_path(entry.run_id).open("a", encoding="utf-8") as handle:
                handle.write(line)
            self._write_manifest(entry.run_id)
            return stored

    def _write_manifest(self, run_id: str) -> pathlib.Path:
        rows = _load_rows(self._run_path(run_id))
        hashes = [str(row["entry_hash"]) for row in rows]
        manifest = {
            "schema": SCHEMA_VERSION,
            "run_id": run_id,
            "count": len(rows),
            "merkle_root": _merkle_root(hashes),
            "entry_hashes": hashes,
            "first_hash": hashes[0] if hashes else None,
            "last_hash": hashes[-1] if hashes else None,
            "updated_at": round(time.time(), 3),
        }
        manifest_path = self.runs_dir / f"{run_id}.manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest_path

    # ------------------------------------------------------------------ 可复算输入（ADR-0028）

    def record_inputs(self, run_id: str, seq: int, arguments: dict[str, Any]) -> pathlib.Path:
        """把这次调用的**规范化输入**写进可复算输入件（`runs/<run_id>.inputs.jsonl`）。

        它**不进链**：链是可对外出示的防篡改件，而金额、商户、订单号属于不该进可出示件的数据。
        两者的绑定靠摘要——`reconstruct()` 会校验 `digest(arguments) == 链上的 args_digest`。
        同一条 (run, seq) 只写一次：一次调用有 pending / done 两次记账，但输入只有一份。
        """
        path = self._inputs_path(run_id)
        with self._lock:
            if seq in self.inputs_for(run_id):
                return path
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"seq": seq, "arguments": arguments}, ensure_ascii=False) + "\n"
                )
        return path

    def inputs_for(self, run_id: str) -> dict[int, dict[str, Any]]:
        """读回输入件 `{seq: arguments}`。**文件不在不是错误**——那是「无法重构」，要如实报告。"""
        rows = _load_rows(self._inputs_path(run_id))
        return {int(row["seq"]): (row.get("arguments") or {}) for row in rows}

    def reconstruct(
        self,
        run_id: str | None = None,
        *,
        evaluator: Callable[[EvidenceEntry, dict[str, Any]], str] | None = None,
    ) -> ReconstructReport:
        """逐条把决策**重算**一遍（ADR-0028 的可重构性）。

        两个数刻意分开报，因为它们回答不同的问题：**前置条件满足率**（证据够不够用来重构）、
        **决策重构率**（重构出来对不对）。把输入件撤下之后，链仍然 `verify` 通过，而前置条件满足率
        归零——这个落差就是文献说的 overclaim gap，也是这套分层存在的理由。
        """
        rows = _load_rows(self._run_path(run_id)) if run_id else _load_rows(self.chain_path)
        inputs = self.inputs_for(run_id or "")
        by_seq = _decision_rows(rows)

        eligible = {seq: row for seq, row in by_seq.items() if row.get("method") in RECONSTRUCTABLE}
        skipped = [
            (seq, f"{row.get('method')} 的决策不是策略给的")
            for seq, row in by_seq.items()
            if seq not in eligible
        ]

        checked: dict[int, dict[str, Any]] = {}
        for seq, row in eligible.items():
            arguments = inputs.get(seq)
            if arguments is None:
                skipped.append((seq, "输入件里没有这条（无法重构）"))
                continue
            if digest(arguments) != row.get("args_digest"):
                skipped.append((seq, "输入件与链上的 args_digest 不符（被改过？）"))
                continue
            checked[seq] = arguments

        mismatches: tuple[int, ...] = ()
        matched: int | None = None
        if evaluator is not None:
            mismatches = tuple(
                sorted(
                    seq
                    for seq, arguments in checked.items()
                    if evaluator(_entry_from_row(by_seq[seq]), arguments) != by_seq[seq].get("decision")
                )
            )
            matched = len(checked) - len(mismatches)

        return ReconstructReport(
            eligible=len(eligible),
            preconditions_met=len(checked),
            matched=matched,
            mismatches=mismatches,
            skipped=tuple(sorted(skipped)),
            detail=f"可重构 {len(checked)}/{len(eligible)} 条；跳过 {len(skipped)} 条",
        )

    def _inputs_path(self, run_id: str) -> pathlib.Path:
        return self.runs_dir / f"{run_id}.inputs.jsonl"

    # ------------------------------------------------------------------ 校验与重放

    def verify(self, run_id: str | None = None) -> VerifyReport:
        """不传 `run_id` 校验整条链（并交叉核对所有运行包）；传了就校验那一次运行。"""
        with self._lock:
            report = self._verify_run(run_id) if run_id is not None else self._verify_chain()
        # 把版本带进 detail：链验不过时，第一件要问的事就是「这条链是哪个版本写的」
        # （字段增加会让旧版本的链重算哈希不符，见 SCHEMA_VERSION）。
        return replace(report, detail=f"{report.detail}（schema v{SCHEMA_VERSION}）")

    def _verify_chain(self) -> VerifyReport:
        rows = _load_rows(self.chain_path)
        ok, broken_at, detail = _verify_rows(rows)
        if not ok:
            return VerifyReport(
                ok=False, entries=len(rows), broken_at=broken_at,
                detail=detail + self._version_hint(),
            )

        on_chain = {str(row["entry_hash"]) for row in rows}
        for manifest_path in sorted(self.runs_dir.glob("*.manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            missing = [leaf for leaf in manifest.get("entry_hashes", []) if leaf not in on_chain]
            if missing:
                return VerifyReport(
                    ok=False,
                    entries=len(rows),
                    detail=f"{manifest_path.name} 里有 {len(missing)} 条记录不在链上（链被截断？）",
                )
        return VerifyReport(ok=True, entries=len(rows), detail="链自洽，且所有运行包都能在链上找到")

    def _version_hint(self) -> str:
        """链验不过时，先排除「版本不同」这个最常见的原因（ADR-0028 的字段演进）。

        哈希覆盖全部字段，所以**加一个字段就会让旧版本的链重算不符**——这时报「被改过？」
        会把人引向错误的方向（实测踩过：504 条链在 broken_at=0 就断了，而链其实没被动过）。
        """
        versions: set[int] = set()
        for manifest_path in self.runs_dir.glob("*.manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(manifest.get("schema"), int):
                versions.add(manifest["schema"])
        older = sorted(v for v in versions if v != SCHEMA_VERSION)
        if not older:
            return ""
        return (
            f"；注意：运行包里有 schema v{older} 写的链，而本版是 v{SCHEMA_VERSION}——"
            "哈希覆盖全部字段，加字段会让旧链重算不符（ADR-0028 的预期行为，不是篡改）。"
            "做实验请用新的证据目录。"
        )

    def _verify_run(self, run_id: str) -> VerifyReport:
        run_path = self._run_path(run_id)
        if not run_path.exists():
            return VerifyReport(ok=False, entries=0, detail=f"没有这次运行的记录：{run_path}")
        rows = _load_rows(run_path)
        on_chain = {str(row["entry_hash"]) for row in _load_rows(self.chain_path)}
        # 首条允许锚定到链上（多运行共用证据目录时，它接的是上一次运行的最后一条）
        ok, broken_at, detail = _verify_rows(rows, anchors=on_chain)
        if not ok:
            return VerifyReport(ok=False, entries=len(rows), broken_at=broken_at, detail=detail)

        manifest_path = self.runs_dir / f"{run_id}.manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            hashes = [str(row["entry_hash"]) for row in rows]
            if manifest.get("count") != len(rows):
                return VerifyReport(
                    ok=False,
                    entries=len(rows),
                    detail=f"条数不符：manifest={manifest.get('count')} 实际={len(rows)}（被截断？）",
                )
            if manifest.get("merkle_root") != _merkle_root(hashes):
                return VerifyReport(ok=False, entries=len(rows), detail="Merkle 根不符")

        missing = [row.get("seq") for row in rows if str(row["entry_hash"]) not in on_chain]
        if missing:
            return VerifyReport(ok=False, entries=len(rows), detail=f"这些记录不在全局链上：{missing}")
        return VerifyReport(ok=True, entries=len(rows), detail="包自洽，且与全局链一致")

    def replay(
        self,
        run_id: str | None = None,
        *,
        evaluator: Callable[[EvidenceEntry], str] | None = None,
    ) -> ReplayReport:
        """重放判定。

        - 不带 `evaluator`：只做**重放前置条件探针**，并给出记录里的决策序列；
        - 带 `evaluator`（Step 4 由执行点传进来，闭包住策略引擎）：逐条重算决策并与记录比对，
          算出的不一致位置进 `mismatches`——那就是 I4 的「决策重构率」。

        证据层**不 import 策略**：评估器是参数，不是依赖（层次干净，也便于测试塞假实现）。
        """
        rows = _load_rows(self._run_path(run_id)) if run_id else _load_rows(self.chain_path)
        # 每次调用取第一条记录（见 `_decision_rows`：只看 pending 会漏掉 resources/read
        # 那种只有 done 的路径）。
        decisions_rows = list(_decision_rows(rows).values())
        decisions = tuple(str(row.get("decision")) for row in decisions_rows)
        preconditions_met = all(_has_preconditions(row) for row in rows) if rows else False

        if evaluator is None:
            return ReplayReport(ok=preconditions_met, decisions=decisions, preconditions_met=preconditions_met)

        mismatches = tuple(
            index
            for index, row in enumerate(decisions_rows)
            if evaluator(_entry_from_row(row)) != row.get("decision")
        )
        return ReplayReport(
            ok=not mismatches and preconditions_met,
            decisions=decisions,
            mismatches=mismatches,
            preconditions_met=preconditions_met,
        )

    # ------------------------------------------------------------------ 交付

    def package(
        self, run_id: str, target: pathlib.Path, *, with_inputs: bool = False
    ) -> pathlib.Path:
        """把一次运行的证据包导出到 `target`（JSONL + manifest），返回 manifest 路径。

        `with_inputs=False`（默认）导出的就是**可对外出示**的那一份：只有摘要，没有金额与商户。
        要连可复算输入一起带走（例如交给评测器重算决策），显式传 `with_inputs=True`——那时
        导出件里会出现支付数据，别随手分发（ADR-0028 的分层）。
        """
        run_path = self._run_path(run_id)
        if not run_path.exists():
            raise FileNotFoundError(f"没有这次运行的记录：{run_path}")
        manifest_path = self._write_manifest(run_id)
        if target.resolve() != self.runs_dir.resolve():
            target.mkdir(parents=True, exist_ok=True)
            (target / f"{run_id}.jsonl").write_text(
                run_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            exported = target / f"{run_id}.manifest.json"
            exported.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
            if with_inputs and self._inputs_path(run_id).exists():
                (target / f"{run_id}.inputs.jsonl").write_text(
                    self._inputs_path(run_id).read_text(encoding="utf-8"), encoding="utf-8"
                )
            return exported
        return manifest_path

    def _run_path(self, run_id: str) -> pathlib.Path:
        return self.runs_dir / f"{run_id}.jsonl"



class EvidenceLog:
    """W2 的临时实现：一条记录一次被拦截的调用（没有哈希链）。

    W3 期间保留它，网关才不会因为证据层正在重写而停摆；`ChainLedger` 到位后替换。
    """

    def __init__(self, path: pathlib.Path, run_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.run_id = run_id
        self._fh = path.open("w", encoding="utf-8")
        self._seq = 0

    def record(self, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        entry = {"seq": self._seq, "ts": round(time.time(), 3), "run_id": self.run_id, **fields}
        self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._fh.flush()
        return entry

    def close(self) -> None:
        self._fh.close()

    @staticmethod
    def load(path: pathlib.Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

