import dataclasses
import json
import subprocess
import time
from pathlib import Path

import pytest

from orchestrator.config import load_config
from orchestrator.cli import (
    parse_args, orchestrate, main, ParsedArgs, interactive, _explain_basis,
    _banner, checkpoint_commit, rollback, ensure_git_repo,
)
from orchestrator.runner import RunResult


def test_banner_renders_box_and_context(tmp_path):
    out = _banner(tmp_path, load_config())
    assert "Relay" in out
    assert "Привет" in out
    assert "╭" in out and "╯" in out  # bordered box
    # every boxed row lines up to the same visual width
    rows = [ln for ln in out.splitlines() if ln.startswith("│")]
    assert rows and len({len(r) for r in rows}) == 1


def test_attempt_note_includes_output_tail():
    from orchestrator.cli import _attempt_note
    note = _attempt_note("L1", "tests-failed",
                         "line1\nFAILED test_x\nAssertionError: 1 != 2\n")
    assert note.startswith("L1: tests-failed")
    assert "AssertionError: 1 != 2" in note  # actual error carried to next level


def test_attempt_note_no_output():
    from orchestrator.cli import _attempt_note
    assert _attempt_note("L0", "exit-code:1", "") == "L0: exit-code:1"


def test_parse_auto_flag_and_subcommands():
    assert parse_args(["--auto", "do", "x"]).auto is True
    assert parse_args(["do", "x"]).auto is False
    assert parse_args(["stats"]).command == "stats"
    assert parse_args(["doctor"]).command == "doctor"


def test_session_auto_mode():
    from orchestrator.cli import _new_session, _session_command, _apply_session
    s = _new_session()
    assert _session_command("/auto", s) is not None and s["auto"] is True
    a = _apply_session(parse_args(["fix bug"]), s)
    assert a.auto is True


def test_stats_summarizes_journal(tmp_path, capsys):
    import dataclasses
    from orchestrator import journal as J
    from orchestrator.cli import _print_stats
    p = tmp_path / "j.jsonl"
    J.append(J.new_entry("t1", "L0", "trivial", "opencode", "m",
                         "success", 0, 0.0, []), p)
    J.append(J.new_entry("t2", "L2", "llm:3", "opencode", "m",
                         "success", 0, 0.005, ["L0->L1", "L1->L2"]), p)
    cfg = dataclasses.replace(load_config(), journal_path=str(p))
    _print_stats(cfg)
    out = capsys.readouterr().out
    assert "Задач: 2" in out
    assert "$0.0050" in out          # total cost
    assert "эскалаций 1" in out      # one entry escalated


def test_init_wizard_codex_writes_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from orchestrator.cli import _init_wizard
    _init_wizard(input_fn=lambda p="": "2")
    cfg = tmp_path / ".orchestrator" / "config.toml"
    assert cfg.exists() and "codex" in cfg.read_text()


def test_init_wizard_openrouter_saves_key(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from orchestrator.cli import _init_wizard
    _init_wizard(input_fn=lambda p="": "1",
                 getpass_fn=lambda p="": "sk-or-test123")
    envf = tmp_path / ".orchestrator" / ".env"
    assert 'OPENROUTER_API_KEY="sk-or-test123"' in envf.read_text()
    # secret file must not be group/world readable
    assert (envf.stat().st_mode & 0o077) == 0


def test_list_models_requires_key(monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from orchestrator.cli import _list_models
    rc = _list_models(load_config())
    assert rc == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_doctor_reports_and_returns_code(capsys):
    from orchestrator.cli import _doctor
    rc = _doctor(load_config())
    out = capsys.readouterr().out
    assert "Relay doctor" in out
    assert "Python" in out and "git" in out
    assert rc in (0, 1)


def test_level_rgb_by_tier():
    from orchestrator.cli import _level_rgb, GREEN, YELLOW, MAGENTA
    cfg = load_config()
    assert _level_rgb(cfg, "L0") == GREEN     # free
    assert _level_rgb(cfg, "L2") == YELLOW    # cheap paid (deepseek)
    assert _level_rgb(cfg, "L3") == MAGENTA   # claude / Pro


def test_undo_resets_to_last_checkpoint(tmp_path):
    # real git repo with an orchestrator checkpoint commit + later change
    def git(*a):
        subprocess.run(["git", *a], cwd=str(tmp_path), capture_output=True)
    git("init")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("original\n")
    git("add", "-A")
    git("commit", "-m", "orchestrator: checkpoint")
    (tmp_path / "f.txt").write_text("changed by agent\n")  # uncommitted "task" edit

    from orchestrator.cli import _repl_command
    assert _repl_command("/undo", load_config(), tmp_path) == ""
    assert (tmp_path / "f.txt").read_text() == "original\n"  # rolled back


def test_repl_command_exit_help_config_unknown(tmp_path, capsys):
    from orchestrator.cli import _repl_command
    cfg = load_config()
    assert _repl_command("/exit", cfg, tmp_path) == "exit"
    assert _repl_command("/quit", cfg, tmp_path) == "exit"
    assert _repl_command("/help", cfg, tmp_path) == ""
    assert _repl_command("/config", cfg, tmp_path) == ""
    assert _repl_command("/nope", cfg, tmp_path) is None
    out = capsys.readouterr().out
    assert "Команды Relay" in out and "Лестница моделей" in out


def test_session_commands_set_defaults():
    from orchestrator.cli import _new_session, _session_command, _apply_session
    s = _new_session()
    assert _session_command("/dry", s) is not None and s["dry_run"] is True
    assert _session_command("/dry off", s) is not None and s["dry_run"] is False
    _session_command("/steps 7", s)
    assert s["max_steps"] == 7
    _session_command("/steps off", s)
    assert s["max_steps"] is None
    _session_command("/test pytest -q", s)
    assert s["test_cmd"] == "pytest -q"
    _session_command("/guard off", s)
    assert s["no_commit_guard"] is True
    assert _session_command("/nope", s) is None  # not a session command


def test_apply_session_folds_defaults_but_inline_wins():
    from orchestrator.cli import _apply_session
    session = {"dry_run": True, "no_commit_guard": False,
               "max_steps": 5, "test_cmd": "pytest", "auto": False}
    # inline line has no max_steps -> takes session's 5; dry_run from session
    a = parse_args(["do", "thing"])
    _apply_session(a, session)
    assert a.dry_run is True and a.max_steps == 5 and a.test_cmd == "pytest"
    # inline --max-steps wins over session
    b = _apply_session(parse_args(["--max-steps", "20", "x"]), session)
    assert b.max_steps == 20


def test_interactive_session_dry_applies_to_next_task(tmp_path):
    cfg = load_config()
    seen = []
    rc = interactive(cfg, tmp_path,
                     input_fn=_line_feeder(["/dry", "do a thing", "/exit"]),
                     dispatch=lambda args, c, r: seen.append(args.dry_run))
    assert rc == 0
    assert seen == [True]  # /dry set the mode; the task ran in dry-run


def test_interactive_handles_slash_command_without_dispatch(tmp_path):
    cfg = load_config()
    dispatched = []
    rc = interactive(cfg, tmp_path,
                     input_fn=_line_feeder(["/help", "/exit"]),
                     dispatch=lambda *a: dispatched.append(a))
    assert rc == 0
    assert dispatched == []  # /help and /exit are commands, never dispatched


def test_explain_basis_readable():
    assert _explain_basis("explicit") == "выбрано вручную"
    assert "3/5" in _explain_basis("llm:3")
    assert "вверх" in _explain_basis("heuristic:up-keyword")
    assert "вниз" in _explain_basis("heuristic:down-keyword")
    assert "2" in _explain_basis("heuristic:files:2")
    assert _explain_basis("something-new") == "something-new"  # fallback


def _line_feeder(lines):
    """input_fn stub: yields each line, then raises EOFError (Ctrl-D)."""
    it = iter(lines)

    def _input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return _input


def test_interactive_dispatches_each_line_then_exits_on_eof(tmp_path):
    cfg = load_config()
    seen = []
    rc = interactive(
        cfg, tmp_path,
        input_fn=_line_feeder(["/l3 fix auth", "  ", "добавь докстринг"]),
        dispatch=lambda args, config, root: seen.append(
            (args.explicit_level, args.task)),
    )
    assert rc == 0
    # blank line skipped; both real tasks dispatched with parsed prefix/task
    assert seen == [("L3", "fix auth"), (None, "добавь докстринг")]


def test_interactive_exit_command_stops(tmp_path):
    cfg = load_config()
    seen = []
    rc = interactive(
        cfg, tmp_path,
        input_fn=_line_feeder(["exit", "this should never run"]),
        dispatch=lambda args, config, root: seen.append(args.task),
    )
    assert rc == 0
    assert seen == []


def test_interactive_survives_a_failing_task(tmp_path):
    cfg = load_config()
    calls = []

    def boom(args, config, root):
        calls.append(args.task)
        if args.task == "bad":
            raise RuntimeError("kaboom")

    rc = interactive(cfg, tmp_path,
                     input_fn=_line_feeder(["bad", "good"]), dispatch=boom)
    assert rc == 0
    assert calls == ["bad", "good"]  # REPL stayed alive after the error


def test_main_bare_opens_repl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # bare `relay`: EOF immediately -> REPL returns 0 without dispatching
    monkeypatch.setattr("builtins.input", _line_feeder([]))
    assert main([]) == 0

CFG = load_config()


def test_parse_prefix_and_flags():
    a = parse_args(["/l3", "fix", "the", "auth", "bug", "--max-steps", "10"])
    assert a.command == "run"
    assert a.explicit_level == "L3"
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
            "L3", "claude", ["claude-x"], "explicit"),
        "run": lambda dec, prompt, cfg, steps, dry_run: RunResult(
            1, "", "", "m", False),
        "detect_failure": lambda res, cfg, root: "exit-code:1",
        "checkpoint": lambda root: "abc",
        "journal_append": lambda entry, path: journal_entries.append(entry),
    }
    args = ParsedArgs("run", "some big task", "L3", None, None, False, True)
    code = orchestrate(args, cfg, tmp_path, deps=deps)

    assert code == 1
    assert journal_entries[-1].outcome.startswith("failed:")


def test_orchestrate_cost_ceiling_returns_6_without_escalating(tmp_path: Path):
    from orchestrator.router import RouteDecision

    journal_entries = []
    escalated = {"n": 0}

    def run(dec, prompt, cfg, steps, dry_run):
        escalated["n"] += 1
        return RunResult(0, "done", "", "m",
                         usage={"cost": 0.99, "input": 1, "output": 1,
                                "context": 1, "reasoning": 0},
                         cost_limit_hit=True)

    cfg = dataclasses.replace(CFG, budget_path=str(tmp_path / "b.json"))
    deps = {
        "classify": lambda task, cfg, **kw: RouteDecision(
            "L2", "opencode", ["m"], "explicit"),
        "run": run,
        "detect_failure": lambda res, cfg, root: None,
        "checkpoint": lambda root: "abc",
        "journal_append": lambda entry, path: journal_entries.append(entry),
    }
    args = ParsedArgs("run", "runaway task", "L2", None, None, False, True)
    code = orchestrate(args, cfg, tmp_path, deps=deps)

    assert code == 6
    assert escalated["n"] == 1                       # did NOT escalate
    assert journal_entries[-1].outcome == "cost-ceiling"
    assert journal_entries[-1].cost_usd == 0.99      # real cost recorded


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
            "L3", "claude", ["claude-x"], "explicit"),
        "run": run,
        "detect_failure": lambda res, cfg, root: None,
        "checkpoint": lambda root: "abc",
        "journal_append": lambda entry, path: journal_entries.append(entry),
    }
    args = ParsedArgs("run", "some task", "L3", None, None, False, True)
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
            "L3", "claude", ["claude-x"], "explicit"),
        "run": run,
        "detect_failure": lambda res, cfg, root: None,
        "checkpoint": lambda root: checkpoint_calls.append(root),
        "journal_append": lambda entry, path: journal_entries.append(entry),
    }
    args = ParsedArgs("run", "redesign x", "L3", None, None, True, False)
    code = orchestrate(args, cfg, tmp_path, deps=deps)

    assert code == 0
    assert run_calls == [True]
    assert checkpoint_calls == []
    assert journal_entries == []

    from orchestrator.budget import ProWindow
    pro = ProWindow(budget_path, cfg.pro_window_hours, cfg.pro_window_max_runs)
    assert pro.runs_in_window() == 0
    assert not budget_path.exists()
