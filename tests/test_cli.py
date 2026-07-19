from pathlib import Path
from orchestrator.config import load_config
from orchestrator.cli import parse_args, orchestrate, ParsedArgs
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
