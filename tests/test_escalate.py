from pathlib import Path
from orchestrator.config import load_config
from orchestrator.runner import RunResult
from orchestrator.escalate import (
    next_level, detect_loop, detect_failure, build_escalation_prompt,
)

CFG = load_config()


def test_next_level():
    assert next_level("L0") == "L1"
    assert next_level("L2") == "L3"
    assert next_level("L3") is None


def test_detect_loop():
    text = "call foo\ncall foo\ncall foo\n"
    assert detect_loop(text, threshold=3) is True
    assert detect_loop("a\nb\nc\n", threshold=3) is False


def test_detect_failure_on_exit_code():
    res = RunResult(1, "", "boom", "m", False)
    assert detect_failure(res, CFG, Path(".")) == "exit-code:1"


def test_detect_failure_on_step_limit():
    res = RunResult(0, "", "", "m", True)
    assert detect_failure(res, CFG, Path(".")) == "step-limit"


def test_detect_failure_none_on_success():
    res = RunResult(0, "all good", "", "m", False)
    assert detect_failure(res, CFG, Path(".")) is None


def test_detect_failure_test_cmd(tmp_path: Path):
    import dataclasses
    cfg = dataclasses.replace(CFG, test_cmd="pytest")
    res = RunResult(0, "", "", "m", False)
    # test command reports failure via injected runner
    reason = detect_failure(res, cfg, tmp_path,
                            _test_runner=lambda cmd, cwd: 1)
    assert reason == "tests-failed"


def test_build_escalation_prompt_includes_diff_and_attempts(tmp_path: Path):
    prompt = build_escalation_prompt(
        "fix auth", ["L1: exit-code:1"], tmp_path,
        _runner=lambda root: "diff --git a/x b/x\n+changed",
    )
    assert "fix auth" in prompt
    assert "L1: exit-code:1" in prompt
    assert "diff --git" in prompt
