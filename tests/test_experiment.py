"""实验机器（`eval/experiment.py`）的单元测试：判据与族语义。"""

from __future__ import annotations

from experiment import Goal, Pattern, judge, load_payloads


def test_numeric_expectation_matches() -> None:
    pattern = Pattern(tool="psp__create_payment_intent", args={"amount": {"gt": 50}})
    assert pattern.matches("psp__create_payment_intent", {"amount": 500})
    assert not pattern.matches("psp__create_payment_intent", {"amount": 50})
    assert not pattern.matches("psp__create_payment_intent", {"amount": "nope"})


def _run(*, completed: bool) -> list[dict]:
    calls: list[dict] = [
        {"type": "tool_call", "name": "merchant__list_catalog", "arguments": {}, "result": {"is_error": False}},
    ]
    if completed:
        calls.append(
            {"type": "tool_call", "name": "merchant__complete_checkout", "arguments": {}, "result": {"is_error": False}}
        )
    calls.append({"type": "run_end", "status": "done", "steps": 2, "tokens": 10})
    return calls


def test_abort_goal_is_hooked_only_when_the_task_is_abandoned() -> None:
    goal = Goal(kind="abort")
    abandoned = judge(trajectory=_run(completed=False), evidence=[], placement="catalog", goal=goal)
    finished = judge(trajectory=_run(completed=True), evidence=[], placement="catalog", goal=goal)
    assert abandoned["hooked"] is True and abandoned["completed"] is False
    assert finished["hooked"] is False and finished["completed"] is True


def test_payload_file_has_both_goal_kinds() -> None:
    payloads = load_payloads()
    assert {level.goal.kind for level in payloads.levels} == {"action", "abort"}
    assert payloads.level("F3_abort/L3").goal.kind == "abort"
    assert payloads.level("F4_forged_authority/L3").goal.patterns


def test_negative_control_stays_out_of_the_default_plan() -> None:
    """F9 的注入组 5/5 只是 fixture 行为；默认跑它会误报 100% ASR。"""
    payloads = load_payloads()
    assert payloads.level("F9_attribute_spoof/L3").default is False
    assert payloads.level("F8_branded_whisper/L3").default is False
    assert payloads.level("F1_prerequisite/L3").default is True

