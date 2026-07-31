"""Regressions for bugs found in review — one test per real failure mode.

Each test here names the behaviour that used to be wrong, so a future change
that reintroduces it fails loudly rather than silently costing money or data.
"""
import json
import subprocess
from pathlib import Path

import pytest

from orchestrator.config import load_config


def git(root: Path, *argv: str):
    return subprocess.run(["git", *argv], cwd=str(root),
                          capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch):
    """A real git repo with one commit, isolated from the user's ~/.orchestrator."""
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "state"))
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("original\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "initial")
    return tmp_path


# --------------------------------------------------------------------------
# /undo used to `git reset --hard` past the user's own commits, destroying them
# --------------------------------------------------------------------------

def test_undo_refuses_to_destroy_user_commits_without_confirmation(repo: Path):
    from orchestrator.cli import checkpoint_commit, record_checkpoint, undo

    sha = checkpoint_commit(repo)
    record_checkpoint(repo, sha)

    (repo / "important.py").write_text("my important work\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "my own work")
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()

    msg = undo(repo, confirm=lambda _q: False)   # user declines

    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (repo / "important.py").exists()      # work survived
    assert "ancel" in msg or "тмен" in msg


def test_undo_destroys_user_commits_only_when_confirmed(repo: Path):
    from orchestrator.cli import checkpoint_commit, record_checkpoint, undo

    sha = checkpoint_commit(repo)
    record_checkpoint(repo, sha)
    (repo / "important.py").write_text("my important work\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "my own work")

    asked = []
    undo(repo, confirm=lambda q: asked.append(q) or True)

    assert asked, "must ask before throwing away the user's commits"
    assert not (repo / "important.py").exists()


def test_undo_rolls_back_agent_edits_without_asking(repo: Path):
    # the common case: only uncommitted task edits -> no friction, no prompt
    from orchestrator.cli import checkpoint_commit, record_checkpoint, undo

    record_checkpoint(repo, checkpoint_commit(repo))
    (repo / "f.txt").write_text("changed by agent\n")

    undo(repo, confirm=lambda _q: pytest.fail("must not prompt here"))

    assert (repo / "f.txt").read_text() == "original\n"


def test_undo_twice_is_not_a_silent_no_op(repo: Path):
    from orchestrator.cli import checkpoint_commit, record_checkpoint, undo

    record_checkpoint(repo, checkpoint_commit(repo))
    (repo / "f.txt").write_text("changed by agent\n")
    undo(repo)

    second = undo(repo)
    assert "↩" not in second, "a second /undo must not claim it rolled back again"


# --------------------------------------------------------------------------
# checkpoint used to `git add -A`, sweeping untracked secrets into a commit
# --------------------------------------------------------------------------

def test_checkpoint_does_not_commit_untracked_secrets(repo: Path):
    from orchestrator.cli import checkpoint_commit

    (repo / ".env.local").write_text("SECRET=abc\n")
    (repo / "app.py").write_text("print(1)\n")

    checkpoint_commit(repo)

    tracked = git(repo, "ls-files").stdout.split()
    assert "app.py" in tracked
    assert ".env.local" not in tracked


def test_checkpoint_does_not_pile_up_empty_commits(repo: Path):
    from orchestrator.cli import checkpoint_commit

    before = git(repo, "rev-list", "--count", "HEAD").stdout.strip()
    for _ in range(3):
        checkpoint_commit(repo)          # clean tree every time
    after = git(repo, "rev-list", "--count", "HEAD").stdout.strip()

    assert before == after               # no junk commits in the user's history


# --------------------------------------------------------------------------
# a malformed flag used to raise StopIteration/ValueError out of parse_args,
# killing the whole REPL session
# --------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["--max-steps"], ["--test-cmd"]])
def test_parse_args_missing_flag_value_raises_value_error(argv):
    from orchestrator.cli import parse_args
    with pytest.raises(ValueError):
        parse_args(argv)


def test_parse_args_non_numeric_max_steps_raises_value_error():
    from orchestrator.cli import parse_args
    with pytest.raises(ValueError):
        parse_args(["--max-steps", "abc"])


def test_main_reports_bad_arguments_instead_of_traceback(capsys):
    from orchestrator.cli import main
    assert main(["--max-steps"]) == 3
    assert "max-steps" in capsys.readouterr().err


def test_interactive_survives_a_malformed_flag(tmp_path: Path):
    from orchestrator.cli import interactive
    lines = iter(["--max-steps", "/help", "exit"])
    dispatched = []
    code = interactive(load_config(), tmp_path,
                       input_fn=lambda _p: next(lines),
                       dispatch=lambda *a: dispatched.append(a))
    assert code == 0            # session survived the bad line and reached exit


def test_double_dash_passes_the_rest_through_as_the_task():
    from orchestrator.cli import parse_args
    args = parse_args(["--", "explain", "--dry-run", "please"])
    assert args.task == "explain --dry-run please"
    assert args.dry_run is False


# --------------------------------------------------------------------------
# routing: stopwords and vendored dirs used to inflate the file count and send
# ordinary tasks to the most expensive level
# --------------------------------------------------------------------------

def test_common_english_words_are_not_grepped():
    from orchestrator.router import significant_tokens
    tokens = significant_tokens("make the help output shorter")
    for filler in ("make", "the", "help"):
        assert filler not in tokens


def test_a_token_matching_most_of_the_repo_is_treated_as_noise(tmp_path: Path):
    from orchestrator.router import estimate_file_count
    for i in range(10):
        (tmp_path / f"mod{i}.py").write_text("# describes the output format\n")
    (tmp_path / "billing_total.py").write_text("billing_total = 1\n")

    # 'output' is in every file -> noise; the real identifier still counts
    assert estimate_file_count("change the output format", tmp_path) == 0
    assert estimate_file_count("fix billing_total", tmp_path) == 1


def test_identifier_like_tokens_still_count():
    from orchestrator.router import significant_tokens
    tokens = significant_tokens("rename parse_args in utils.py and BillingJob")
    assert "parse_args" in tokens and "utils" in tokens and "BillingJob" in tokens


def test_ordinary_english_task_does_not_route_to_the_top_level(tmp_path: Path):
    from orchestrator.router import heuristic_level
    for i in range(10):
        (tmp_path / f"mod{i}.py").write_text("# the help output for the user\n")

    level, reason = heuristic_level("make the help output shorter", tmp_path, False)
    assert level != "L3", f"stopwords still inflate the file count ({reason})"


def test_vendored_directories_do_not_inflate_the_file_count(tmp_path: Path):
    from orchestrator.router import estimate_file_count
    for d in (".git", "node_modules", ".venv", "__pycache__"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "x.py").write_text("billing_total = 1\n")

    assert estimate_file_count("fix billing_total", tmp_path) == 0


def test_questions_start_at_the_cheapest_level(tmp_path: Path):
    from orchestrator.router import heuristic_level
    for q in ("explain what this project does",
              "why does the prompt look wrong?",
              "что делает этот модуль"):
        level, reason = heuristic_level(q, tmp_path, False)
        assert level == "L0", f"{q!r} routed to {level} ({reason})"


# --------------------------------------------------------------------------
# runner: framework error events were parsed and silently dropped, so failures
# printed nothing and model rotation could never fire
# --------------------------------------------------------------------------

def test_opencode_error_event_is_shown_to_the_user():
    from orchestrator.runner import _StreamParser
    evt = json.dumps({"type": "error", "error": {
        "name": "UnknownError",
        "data": {"message": "Unexpected server error. Check server logs."}}})
    written = []
    parser = _StreamParser("opencode")
    parser.feed(evt, written.append)

    assert "Unexpected server error" in "".join(written)
    assert "Unexpected server error" in parser.text   # and reaches the journal


def test_claude_error_result_is_shown_to_the_user():
    from orchestrator.runner import _StreamParser
    evt = json.dumps({"type": "result", "subtype": "error_during_execution",
                      "is_error": True, "result": "model overloaded"})
    written = []
    parser = _StreamParser("claude")
    parser.feed(evt, written.append)
    assert "model overloaded" in "".join(written)


def test_rotation_fires_on_a_json_rate_limit_event():
    from orchestrator.runner import _StreamParser, _classify_model_error
    evt = json.dumps({"type": "error", "error": {
        "data": {"message": "Rate limit exceeded (429) for this model"}}})
    parser = _StreamParser("opencode")
    parser.feed(evt, lambda _s: None)

    assert _classify_model_error(parser.text) == "rate-limit"


def test_rotation_fires_when_a_model_produces_nothing_at_all():
    from orchestrator.runner import _is_model_fault
    # a dead model id: non-zero exit, no steps, no assistant text -> rotate
    assert _is_model_fault("", 1, {"steps": 0, "assistant_chars": 0}) == "no-output"
    # a genuine task failure that did work must NOT rotate
    assert _is_model_fault("tried and failed", 1,
                           {"steps": 4, "assistant_chars": 60}) is None


def test_surfaced_error_text_does_not_mask_a_dead_model():
    """The error-visibility fix must not defeat the rotation check.

    The error message lands in captured output, so a naive "did it print
    anything?" test would see text and refuse to rotate — which is exactly
    what a dead model looks like.
    """
    from orchestrator.runner import _StreamParser, _is_model_fault
    evt = json.dumps({"type": "error", "error": {
        "data": {"message": "Unexpected server error. Check server logs."}}})
    parser = _StreamParser("opencode")
    parser.feed(evt, lambda _s: None)

    assert parser.text.strip()                      # the user does see it
    assert _is_model_fault(parser.text, 1, parser.usage) == "no-output"


def test_dry_run_preview_includes_auto_flags():
    from orchestrator.runner import run_framework
    from orchestrator.router import RouteDecision
    import io
    import contextlib

    cfg = load_config()
    dec = RouteDecision("L0", "opencode", ["m1"], "explicit")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_framework(dec, "task", cfg, 40, dry_run=True, auto=True)

    assert "--auto" in buf.getvalue()   # preview matches what would really run


# --------------------------------------------------------------------------
# task_timeout_seconds was a total run cap, so a long but healthy task got
# killed and escalated to a pricier level; it is now an idle timeout
# --------------------------------------------------------------------------

def test_watchdog_spares_a_slow_but_talking_process():
    import subprocess as sp
    import sys
    from orchestrator.runner import _IdleWatchdog

    script = ("import time,sys\n"
              "for i in range(6):\n"
              "    print(i, flush=True); time.sleep(0.1)\n")
    proc = sp.Popen([sys.executable, "-c", script], stdout=sp.PIPE, text=True)
    with _IdleWatchdog(proc, timeout=0.3) as w:   # < total runtime (0.6s)
        for _ in proc.stdout:
            w.ping()
    proc.wait()

    assert not w.fired          # kept talking -> must survive
    assert proc.returncode == 0


def test_watchdog_kills_a_silent_process():
    import subprocess as sp
    import sys
    from orchestrator.runner import _IdleWatchdog

    proc = sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                    stdout=sp.PIPE, text=True)
    with _IdleWatchdog(proc, timeout=0.5) as w:
        for _ in proc.stdout:
            w.ping()
    proc.wait()

    assert w.fired


# --------------------------------------------------------------------------
# loop detection used to fire on any 3 repeated lines
# --------------------------------------------------------------------------

def test_repeated_tool_markers_are_not_a_loop():
    from orchestrator.escalate import detect_loop
    out = "\n".join(["⚙ read", "some real progress here"] * 3
                    + [f"line {i} of actual work" for i in range(20)])
    assert detect_loop(out) is False


def test_a_genuine_loop_is_still_detected():
    from orchestrator.escalate import detect_loop
    assert detect_loop("retrying the same thing\n" * 6) is True


# --------------------------------------------------------------------------
# a config with fewer levels used to crash with a raw KeyError
# --------------------------------------------------------------------------

SHORT_CONFIG = """
max_steps = 5
task_timeout_seconds = 3
cost_ceiling_usd = 0.5
pro_window_hours = 5.0
pro_window_max_runs = 2
classifier_model = ""
journal_path = "{jp}"
budget_path = "{bp}"
test_cmd = ""

[frameworks.fake]
cmd = 'fake {{model}} "{{prompt}}"'
format = "text"
auto = []

[levels.L0]
models = ["a"]
price_in = 0.0
price_out = 0.0
framework = "fake"

[levels.L1]
models = ["b"]
price_in = 0.0
price_out = 0.0
framework = "fake"
"""


@pytest.fixture
def short_config(tmp_path: Path):
    path = tmp_path / "short.toml"
    path.write_text(SHORT_CONFIG.format(jp=tmp_path / "j.jsonl",
                                        bp=tmp_path / "b.json"))
    return load_config(path)


def test_ladder_reflects_the_levels_the_config_defines(short_config):
    assert short_config.ladder == ["L0", "L1"]


def test_banner_renders_for_a_short_ladder(short_config, tmp_path: Path):
    from orchestrator.cli import _banner
    out = _banner(tmp_path, short_config)      # used to raise KeyError: 'L3'
    assert "L0" in out and "L1" in out


def test_config_screens_render_for_a_short_ladder(short_config, tmp_path, capsys):
    from orchestrator.cli import _print_config
    _print_config(short_config, tmp_path)
    assert "L1" in capsys.readouterr().out


def test_escalation_stops_at_the_top_of_a_short_ladder(short_config):
    from orchestrator.escalate import next_level
    assert next_level("L0", short_config.ladder) == "L1"
    assert next_level("L1", short_config.ladder) is None


def test_forcing_a_level_the_config_lacks_is_a_clean_error(short_config, tmp_path,
                                                           capsys):
    from orchestrator.cli import _dispatch, parse_args
    code = _dispatch(parse_args(["/l3", "do", "a", "thing", "--no-commit-guard"]),
                     short_config, tmp_path)
    assert code == 3
    assert "L3" in capsys.readouterr().err


# --------------------------------------------------------------------------
# the journal recorded 0.0 cost for failed runs and never counted the cost of
# intermediate escalation attempts, so /stats understated real spend
# --------------------------------------------------------------------------

def _escalating_deps(journal, outcomes):
    """Deps whose runs cost $0.01 and 2 steps each, failing per `outcomes`."""
    from orchestrator.router import RouteDecision
    from orchestrator.runner import RunResult

    def run(dec, prompt, cfg, steps, dry_run):
        return RunResult(0, "tried", "", "m", False,
                         usage={"cost": 0.01, "steps": 2})

    return {
        "classify": lambda task, cfg, **kw: RouteDecision(
            kw.get("explicit_level") or "L0", "opencode", ["m"], "explicit"),
        "run": run,
        "detect_failure": lambda res, cfg, root: outcomes.pop(0),
        "checkpoint": lambda root: "abc",
        "journal_append": lambda entry, path: journal.append(entry),
    }


def test_journal_records_cost_of_every_escalation_attempt(tmp_path: Path):
    from orchestrator.cli import ParsedArgs, orchestrate

    journal = []
    deps = _escalating_deps(journal, ["exit-code:1", "exit-code:1", None])
    args = ParsedArgs("run", "hard task", "L0", None, None, False, True)

    assert orchestrate(args, load_config(), tmp_path, deps=deps) == 0
    entry = journal[-1]
    assert round(entry.cost_usd, 4) == 0.03   # all three legs, not just the last
    assert entry.steps == 6                   # steps are recorded, not hardcoded 0


def test_journal_records_cost_when_the_task_fails_at_the_top(tmp_path: Path):
    from orchestrator.cli import ParsedArgs, orchestrate

    journal = []
    deps = _escalating_deps(journal, ["exit-code:1"] * 4)
    args = ParsedArgs("run", "hard task", "L0", None, None, False, True)

    assert orchestrate(args, load_config(), tmp_path, deps=deps) == 1
    assert journal[-1].cost_usd > 0    # used to be a hardcoded 0.0
    assert journal[-1].outcome.startswith("failed:")


def test_escalated_level_is_not_labelled_forced(tmp_path: Path, capsys):
    from orchestrator.cli import ParsedArgs, orchestrate

    journal = []
    deps = _escalating_deps(journal, ["exit-code:1", None])
    args = ParsedArgs("run", "hard task", "L0", None, None, False, True)
    orchestrate(args, load_config(), tmp_path, deps=deps)

    out = capsys.readouterr().out
    assert "escalated from L0" in out
    assert journal[-1].basis == "escalated:L0"


# --------------------------------------------------------------------------
# `/test off` could not override a test_cmd coming from the config
# --------------------------------------------------------------------------

def test_session_test_off_overrides_config_test_cmd():
    from orchestrator.cli import _new_session, _session_command, _apply_session
    from orchestrator.cli import ParsedArgs

    session = _new_session()
    _session_command("/test off", session)
    args = _apply_session(
        ParsedArgs("run", "t", None, None, None, False, False), session)

    assert args.test_cmd == ""      # explicit off, not "unset" (which None means)


def test_config_with_undefined_framework_is_rejected(tmp_path: Path):
    bad = tmp_path / "bad.toml"
    bad.write_text(SHORT_CONFIG.format(jp="j", bp="b")
                   .replace('framework = "fake"', 'framework = "ghost"', 1))
    with pytest.raises(ValueError, match="ghost"):
        load_config(bad)
