import dataclasses
import json
import subprocess
import time
from pathlib import Path

import pytest

from orchestrator.config import load_config
from orchestrator.cli import (
    parse_args, orchestrate, main, ParsedArgs,
    checkpoint_commit, rollback, ensure_git_repo,
)
from orchestrator.runner import RunResult

CFG = load_config()


def test_parse_prefix_and_flags():
    a = parse_args(["/l4", "fix", "the", "auth", "bug", "--max-steps", "10"])
    assert a.command == "run"
    assert a.explicit_level == "L4"
    assert a.task == "fix the auth bug"
    assert a.max_steps == 10


def test_parse_no_prefix():
    a = parse_args(["rename", "variable", "x"])
    assert a.explicit_level is None
    assert a.task == "rename variable x"


def test_parse_dry_run_and_subcommand():
    a = parse_args(["--dry-run", "do", "thing"])
    assert a.dry_run is True
    r = parse_args(["rollback"])
    assert r.command == "rollback"


def test_orchestrate_success_path(tmp_path: Path):
    # inject deps so no real subprocess/network happens
    calls = {"journal": []}
    deps = {
        "classify": lambda task, cfg, **kw: __import__(
            "orchestrator.router", fromlist=["RouteDecision"]
        ).RouteDecision("L0", "opencode", ["m"], "explicit"),
        "run": lambda dec, prompt, cfg, steps, dry_run: RunResult(0, "ok", "", "m", False),
        "detect_failure": lambda res, cfg, root: None,
        "checkpoint": lambda root: "abc123",
        "journal_append": lambda entry, path: calls["journal"].append(entry),
    }
    args = ParsedArgs("run", "do a thing", "L0", None, None, False, True)
    code = orchestrate(args, CFG, tmp_path, deps=deps)
    assert code == 0
    assert len(calls["journal"]) == 1
    assert calls["journal"][0].outcome == "success"
    assert calls["journal"][0].level == "L0"


def test_orchestrate_escalates_on_failure(tmp_path: Path):
    from orchestrator.router import RouteDecision
    attempts = {"n": 0}
    journal = []

    def run(dec, prompt, cfg, steps, dry_run):
        attempts["n"] += 1
        # L0 fails, L1 succeeds
        return RunResult(0 if dec.level == "L1" else 1, "", "", "m", False)

    deps = {
        "classify": lambda task, cfg, **kw: RouteDecision(
            kw.get("explicit_level") or "L0", "opencode", ["m"], "explicit"),
        "run": run,
        "detect_failure": lambda res, cfg, root: (
            None if res.exit_code == 0 else "exit-code:1"),
        "checkpoint": lambda root: "abc",
        "journal_append": lambda entry, path: journal.append(entry),
    }
    args = ParsedArgs("run", "hard task", "L0", None, None, False, True)
    code = orchestrate(args, CFG, tmp_path, deps=deps)
    assert code == 0
    assert attempts["n"] == 2          # escalated L0 -> L1
    assert journal[-1].level == "L1"
    assert journal[-1].escalations == ["L0->L1"]


def test_checkpoint_commit_failure_raises(tmp_path: Path):
    calls = []

    class FakeProc:
        def __init__(self, returncode, stdout=""):
            self.returncode = returncode
            self.stdout = stdout

    def fake_runner(argv):
        calls.append(argv)
        if argv[:2] == ["git", "commit"]:
            return FakeProc(1, "")
        return FakeProc(0, "deadbeef\n")

    with pytest.raises(RuntimeError):
        checkpoint_commit(tmp_path, _runner=fake_runner)

    # must not have gone on to call rev-parse and return a bogus sha
    assert ["git", "rev-parse", "HEAD"] not in calls


def test_checkpoint_commit_success(tmp_path: Path):
    calls = []

    class FakeProc:
        def __init__(self, returncode, stdout=""):
            self.returncode = returncode
            self.stdout = stdout

    def fake_runner(argv):
        calls.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return FakeProc(0, "deadbeef123\n")
        return FakeProc(0, "")

    sha = checkpoint_commit(tmp_path, _runner=fake_runner)

    assert calls[0] == ["git", "add", "-A"]
    assert calls[1][:2] == ["git", "commit"]
    assert calls[2] == ["git", "rev-parse", "HEAD"]
    assert sha == "deadbeef123"


def test_rollback_invokes_git_reset(tmp_path: Path):
    calls = []

    def fake_runner(argv):
        calls.append(argv)

    rollback(tmp_path, "deadbeef123", _runner=fake_runner)

    assert calls == [["git", "reset", "--hard", "deadbeef123"]]


def test_ensure_git_repo_true_for_real_repo(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True,
                   text=True)
    assert ensure_git_repo(tmp_path) is True


def test_ensure_git_repo_false_for_non_repo(tmp_path: Path):
    sub = tmp_path / "plain_dir"
    sub.mkdir()
    assert ensure_git_repo(sub) is False


def test_orchestrate_top_of_ladder_failure_returns_1(tmp_path: Path):
    from orchestrator.router import RouteDecision

    journal_entries = []
    cfg = dataclasses.replace(CFG, budget_path=str(tmp_path / "budget.json"))
    deps = {
        "classify": lambda task, cfg, **kw: RouteDecision(
            "L4", "claude", ["claude-x"], "explicit"),
        "run": lambda dec, prompt, cfg, steps, dry_run: RunResult(
            1, "", "", "m", False),
        "detect_failure": lambda res, cfg, root: "exit-code:1",
        "checkpoint": lambda root: "abc",
        "journal_append": lambda entry, path: journal_entries.append(entry),
    }
    args = ParsedArgs("run", "some big task", "L4", None, None, False, True)
    code = orchestrate(args, cfg, tmp_path, deps=deps)

    assert code == 1
    assert journal_entries[-1].outcome.startswith("failed:")


def test_orchestrate_pro_exhausted_returns_2(tmp_path: Path):
    from orchestrator.router import RouteDecision

    budget_file = tmp_path / "budget.json"
    now = time.time()
    budget_file.write_text(json.dumps([now, now, now]))
    cfg = dataclasses.replace(CFG, budget_path=str(budget_file),
                              pro_window_max_runs=2, pro_window_hours=5.0)

    journal_entries = []
    run_called = {"n": 0}

    def run(dec, prompt, cfg, steps, dry_run):
        run_called["n"] += 1
        return RunResult(0, "", "", "m", False)

    deps = {
        "classify": lambda task, cfg, **kw: RouteDecision(
            "L4", "claude", ["claude-x"], "explicit"),
        "run": run,
        "detect_failure": lambda res, cfg, root: None,
        "checkpoint": lambda root: "abc",
        "journal_append": lambda entry, path: journal_entries.append(entry),
    }
    args = ParsedArgs("run", "some task", "L4", None, None, False, True)
    code = orchestrate(args, cfg, tmp_path, deps=deps)

    assert code == 2
    assert journal_entries[-1].outcome == "pro-exhausted"
    assert run_called["n"] == 0


def test_main_empty_task_guard_returns_3():
    code = main(["--no-commit-guard"])
    assert code == 3


def test_main_non_git_repo_guard_returns_3(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = main(["do", "thing"])
    assert code == 3


def test_main_checkpoint_failure_returns_5_not_traceback(
        tmp_path: Path, monkeypatch, capsys):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True,
                   text=True)
    monkeypatch.chdir(tmp_path)

    def boom(root):
        raise RuntimeError("boom")

    monkeypatch.setattr("orchestrator.cli.checkpoint_commit", boom)

    code = main(["do", "thing"])

    assert code == 5
    captured = capsys.readouterr()
    assert "boom" in captured.err


def test_orchestrate_dry_run_l4_does_not_touch_budget_or_journal(
        tmp_path: Path):
    from orchestrator.router import RouteDecision

    budget_path = tmp_path / "budget.json"
    cfg = dataclasses.replace(CFG, budget_path=str(budget_path))

    journal_entries = []
    checkpoint_calls = []
    run_calls = []

    def run(dec, prompt, cfg, steps, dry_run):
        run_calls.append(dry_run)
        return RunResult(0, "[dry-run] would run", "", "m", True)

    deps = {
        "classify": lambda task, cfg, **kw: RouteDecision(
            "L4", "claude", ["claude-x"], "explicit"),
        "run": run,
        "detect_failure": lambda res, cfg, root: None,
        "checkpoint": lambda root: checkpoint_calls.append(root),
        "journal_append": lambda entry, path: journal_entries.append(entry),
    }
    args = ParsedArgs("run", "redesign x", "L4", None, None, True, False)
    code = orchestrate(args, cfg, tmp_path, deps=deps)

    assert code == 0
    assert run_calls == [True]
    assert checkpoint_calls == []
    assert journal_entries == []

    from orchestrator.budget import ProWindow
    pro = ProWindow(budget_path, cfg.pro_window_hours, cfg.pro_window_max_runs)
    assert pro.runs_in_window() == 0
    assert not budget_path.exists()
