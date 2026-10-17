"""证据链的测试（ADR-0003）——I3 与 I4 的来源。

三件必须成立的事：① 能检出**单条篡改**；② 能检出**截断**（包内截断、链被截断）；
③ 未篡改的必须通过——否则「总是失败」的 verify 会假装自己很安全。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from gateway.evidence import GENESIS, ChainLedger, EvidenceEntry


def _entry(**over: object) -> EvidenceEntry:
    base: dict[str, object] = {
        "phase": "pending",
        "seq": 1,
        "run_id": "r1",
        "ts": 0.0,
        "session_id": "s1",
        "method": "tools/call",
        "decision": "allow",
        "tool_call_id": "t1",
        "tool": "merchant__list_catalog",
        "args_digest": "d0",
        "policy_digest": "p0",
        "budget_before": 0.0,
    }
    base.update(over)
    return EvidenceEntry(**base)  # type: ignore[arg-type]


def _ledger(tmp_path: pathlib.Path) -> ChainLedger:
    return ChainLedger(tmp_path / "evidence")


def _seed(ledger: ChainLedger) -> None:
    """一次允许的调用：pending + done。"""
    ledger.record_pending(_entry(seq=1))
    ledger.record_done(_entry(seq=1, phase="done", outcome="ok", result_digest="d1", budget_after=9.71))


def _rewrite(path: pathlib.Path, mutate) -> None:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    mutate(rows)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def test_chain_links_entries(tmp_path: pathlib.Path) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.record_pending(_entry(seq=1))
    second = ledger.record_pending(_entry(seq=2, tool_call_id="t2"))
    assert first.prev_hash == GENESIS
    assert second.prev_hash == first.entry_hash
    assert first.entry_hash != second.entry_hash


def test_verify_passes_on_untampered_run(tmp_path: pathlib.Path) -> None:
    """防假阳性：没动过的证据必须通过（链与包各查一次）。"""
    ledger = _ledger(tmp_path)
    _seed(ledger)
    assert ledger.verify("r1").ok
    assert ledger.verify("r1").entries == 2
    assert ledger.verify().ok


def test_verify_detects_tampering(tmp_path: pathlib.Path) -> None:
    """I3：翻转**运行包里**一条记录的字段，包级校验必须失败并指出位置。"""
    ledger = _ledger(tmp_path)
    _seed(ledger)
    _rewrite(ledger.path / "runs" / "r1.jsonl", lambda rows: rows[0].update({"decision": "deny"}))

    report = ledger.verify("r1")
    assert not report.ok
    assert report.broken_at == 0
    assert "entry_hash" in report.detail
    # 链**没**被动过：链级校验仍然通过。链才是权威，包只是它的一个投影。
    assert ledger.verify().ok


def test_verify_detects_chain_tampering(tmp_path: pathlib.Path) -> None:
    """改链上那一条：链级校验失败，并指出位置。"""
    ledger = _ledger(tmp_path)
    _seed(ledger)
    _rewrite(ledger.path / "chain.jsonl", lambda rows: rows[0].update({"decision": "deny"}))

    report = ledger.verify()
    assert not report.ok
    assert report.broken_at == 0


def test_verify_detects_package_truncation(tmp_path: pathlib.Path) -> None:
    """包内截断：manifest 记了条数与根，少一条就对不上。"""
    ledger = _ledger(tmp_path)
    _seed(ledger)
    run_path = ledger.path / "runs" / "r1.jsonl"
    lines = run_path.read_text(encoding="utf-8").splitlines()
    run_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    report = ledger.verify("r1")
    assert not report.ok
    assert "条数不符" in report.detail


def test_verify_detects_chain_truncation(tmp_path: pathlib.Path) -> None:
    """链被截断：链自己看着自洽，但运行包里的叶哈希在链上找不到了（互为见证）。"""
    ledger = _ledger(tmp_path)
    _seed(ledger)
    chain_path = ledger.path / "chain.jsonl"
    lines = chain_path.read_text(encoding="utf-8").splitlines()
    chain_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    assert not ledger.verify("r1").ok
    report = ledger.verify()
    assert not report.ok
    assert "不在链上" in report.detail


def test_duplicate_record_is_rejected(tmp_path: pathlib.Path) -> None:
    """同一 (run, seq, phase) 重复记录要报错——否则同一次调用会被记两遍。"""
    ledger = _ledger(tmp_path)
    ledger.record_pending(_entry(seq=1))
    with pytest.raises(ValueError, match="重复记录"):
        ledger.record_pending(_entry(seq=1))


def test_deny_is_recorded_too(tmp_path: pathlib.Path) -> None:
    """威胁 T9：拒绝也要留痕，而且要在链上。"""
    ledger = _ledger(tmp_path)
    stored = ledger.record_pending(_entry(seq=1, decision="deny", reason_code="policy_denied"))
    assert stored.entry_hash is not None
    assert ledger.verify().ok
    assert ledger.verify().entries == 1


def test_replay_reports_preconditions(tmp_path: pathlib.Path) -> None:
    """重放前置条件探针：缺字段就必须报出来（I4 的口径）。"""
    ledger = _ledger(tmp_path)
    _seed(ledger)
    assert ledger.replay("r1").preconditions_met

    bare = _ledger(tmp_path / "bare")
    bare.record_pending(_entry(seq=1, policy_digest=None))
    assert not bare.replay("r1").preconditions_met


def test_replay_with_evaluator_counts_mismatches(tmp_path: pathlib.Path) -> None:
    """带评估器时算「决策重构率」：不一致的位置进 mismatches。"""
    ledger = _ledger(tmp_path)
    _seed(ledger)
    assert ledger.replay("r1", evaluator=lambda _entry: "allow").mismatches == ()

    report = ledger.replay("r1", evaluator=lambda _entry: "deny")
    assert report.mismatches == (0,)
    assert not report.ok


def test_replay_decisions_are_one_per_call(tmp_path: pathlib.Path) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger)
    ledger.record_pending(_entry(seq=2, tool_call_id="t2", decision="deny", reason_code="policy_denied"))
    assert ledger.replay("r1").decisions == ("allow", "deny")


def test_package_exports_the_run(tmp_path: pathlib.Path) -> None:
    ledger = _ledger(tmp_path)
    _seed(ledger)
    target = tmp_path / "export"
    manifest_path = ledger.package("r1", target)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["count"] == 2
    assert (target / "r1.jsonl").exists()
    assert manifest["entry_hashes"][0] == manifest["first_hash"]
    assert ledger.verify("r1").ok


def test_verify_missing_run_is_reported(tmp_path: pathlib.Path) -> None:
    report = _ledger(tmp_path).verify("nope")
    assert not report.ok
    assert "没有这次运行的记录" in report.detail

def test_two_runs_share_one_evidence_directory(tmp_path: pathlib.Path) -> None:
    """**回归**：一个证据目录里跑多次运行，全局链必须仍然自洽。

    这条 bug 是 W4 跑 S0 时暴露的：`_append` 从**运行文件**取 prev_hash，于是同一个证据目录里
    第二次运行的第一条会接在 genesis 上，链在边界处断开（实测 `broken_at=11`，345 条）。
    单运行进程（每次演示一个 run_id）永远看不出来——**只有实验这种「多运行共用目录」才会碰到**。
    """
    ledger = _ledger(tmp_path)
    _seed(ledger)                                  # 第一次运行
    ledger.record_pending(_entry(seq=1, run_id="r2", tool_call_id="t2"))   # 第二次运行
    ledger.record_done(_entry(seq=1, run_id="r2", phase="done", outcome="ok", result_digest="d2"))

    report = ledger.verify()
    assert report.ok, report.detail
    assert ledger.verify("r2").ok, ledger.verify("r2").detail


