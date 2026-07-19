import pytest
from orchestrator.config import load_config
from orchestrator.router import RouteDecision
from orchestrator.runner import (
    run_framework, build_command, RunResult, FrameworkNotFound,
)

CFG = load_config()


def test_build_command_substitutes():
    argv = build_command('opencode run -m {model} "{prompt}"',
                         "minimax/minimax-m3", "fix bug", 40)
    assert argv[:3] == ["opencode", "run", "-m"]
    assert "minimax/minimax-m3" in argv
    assert "fix bug" in argv


def test_dry_run_does_not_spawn():
    dec = RouteDecision("L2", "opencode", ["minimax/minimax-m3"], "explicit")
    called = []
    res = run_framework(dec, "task", CFG, 40, dry_run=True,
                        _runner=lambda argv: called.append(argv))
    assert res.exit_code == 0
    assert called == []  # nothing spawned


def test_runner_returns_result():
    dec = RouteDecision("L2", "opencode", ["m1"], "explicit")
    res = run_framework(dec, "task", CFG, 40,
                        _runner=lambda argv: (0, "done", ""))
    assert isinstance(res, RunResult)
    assert res.exit_code == 0
    assert res.model == "m1"


def test_runner_tries_fallback_on_missing_binary():
    dec = RouteDecision("L2", "opencode", ["m1", "m2"], "explicit")
    calls = []

    def runner(argv):
        calls.append(argv)
        if "m1" in argv:
            raise FileNotFoundError("opencode")
        return (0, "ok", "")

    res = run_framework(dec, "task", CFG, 40, _runner=runner)
    assert res.model == "m2"
    assert len(calls) == 2


def test_runner_raises_when_all_missing():
    dec = RouteDecision("L2", "opencode", ["m1"], "explicit")

    def runner(argv):
        raise FileNotFoundError("opencode")

    with pytest.raises(FrameworkNotFound):
        run_framework(dec, "task", CFG, 40, _runner=runner)
