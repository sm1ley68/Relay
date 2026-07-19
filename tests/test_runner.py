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


def test_build_command_prompt_with_shell_metachars_is_single_token():
    prompt = 'fix "auth"; rm -rf ~ && echo x'
    argv = build_command('opencode run -m {model} "{prompt}"',
                         "minimax/minimax-m3", prompt, 40)
    assert prompt in argv
    for bad in (";", "rm", "-rf", "&&", "echo", "x", "~"):
        assert bad not in argv


def test_build_command_rejects_prompt_flag_smuggling():
    with pytest.raises(ValueError):
        build_command('opencode run -m {model} "{prompt}"',
                      "minimax/minimax-m3", "--add-dir", 40)


def test_build_command_rejects_prompt_starting_with_dash():
    with pytest.raises(ValueError):
        build_command('opencode run -m {model} "{prompt}"',
                      "minimax/minimax-m3", "-x", 40)


def test_build_command_rejects_model_starting_with_dash():
    with pytest.raises(ValueError):
        build_command('opencode run -m {model} "{prompt}"',
                      "--evil-flag", "fix bug", 40)


def test_build_command_preserves_literal_steps_placeholder():
    prompt = "please respect {steps} in your plan"
    argv = build_command('opencode run -m {model} "{prompt}"',
                         "minimax/minimax-m3", prompt, 40)
    assert prompt in argv


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
