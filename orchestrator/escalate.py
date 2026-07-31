from __future__ import annotations

import shlex
import subprocess
from collections import Counter
from pathlib import Path

from .config import LADDER, Config
from .runner import RunResult


def next_level(level: str, ladder: list[str] | None = None) -> str | None:
    rungs = ladder or LADDER
    if level not in rungs:
        return None
    idx = rungs.index(level)
    return rungs[idx + 1] if idx + 1 < len(rungs) else None


# A repeated line only means "stuck" if it dominates the output. Agents legally
# repeat short lines (tool markers, separators, blank-ish frames) while making
# real progress — counting those as a loop escalates a healthy run to a pricier
# level for nothing.
_LOOP_MIN_LINE_LEN = 4
_LOOP_MIN_SHARE = 0.30


def detect_loop(stdout: str, threshold: int = 4) -> bool:
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return False
    candidates = [ln for ln in lines if len(ln) >= _LOOP_MIN_LINE_LEN]
    if not candidates:
        return False
    _, count = Counter(candidates).most_common(1)[0]
    return count >= threshold and count / len(lines) >= _LOOP_MIN_SHARE


def _run_test_cmd(cmd: str, cwd: Path) -> int:
    proc = subprocess.run(shlex.split(cmd), cwd=str(cwd),
                          capture_output=True, text=True)
    return proc.returncode


def detect_failure(result: RunResult, config: Config, repo_root: Path, *,
                   _test_runner=None) -> str | None:
    # step-limit / timeout are checked first: when we terminate the process,
    # its exit code is a signal, which would otherwise mask them.
    if result.step_limit_hit:
        return "step-limit"
    if getattr(result, "timeout_hit", False):
        return "timeout"
    if result.exit_code != 0:
        return f"exit-code:{result.exit_code}"
    if detect_loop(result.stdout):
        return "loop"
    if config.test_cmd:
        runner = _test_runner or _run_test_cmd
        if runner(config.test_cmd, repo_root) != 0:
            return "tests-failed"
    return None


def git_diff(repo_root: Path, *, _runner=None) -> str:
    if _runner is not None:
        return _runner(repo_root)
    proc = subprocess.run(["git", "diff"], cwd=str(repo_root),
                          capture_output=True, text=True)
    return proc.stdout


def build_escalation_prompt(original_task: str, attempts: list[str],
                            repo_root: Path, *, _runner=None) -> str:
    diff = git_diff(repo_root, _runner=_runner)
    attempt_lines = "\n".join(f"- {a}" for a in attempts)
    return (
        f"Original task:\n{original_task}\n\n"
        f"Previous attempts failed:\n{attempt_lines}\n\n"
        f"Current state (git diff):\n{diff}\n\n"
        "The files are already on disk. Continue from this state and finish the task."
    )
