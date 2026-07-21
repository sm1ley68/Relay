from __future__ import annotations

import shlex
import subprocess
from collections import Counter
from pathlib import Path

from .config import LADDER, Config
from .runner import RunResult


def next_level(level: str) -> str | None:
    idx = LADDER.index(level)
    return LADDER[idx + 1] if idx + 1 < len(LADDER) else None


def detect_loop(stdout: str, threshold: int = 3) -> bool:
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return False
    counts = Counter(lines)
    return max(counts.values()) >= threshold


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
        f"Исходная задача:\n{original_task}\n\n"
        f"Предыдущие попытки провалились:\n{attempt_lines}\n\n"
        f"Текущее состояние (git diff):\n{diff}\n\n"
        "Файлы уже на диске. Продолжи с этого состояния и доведи задачу до конца."
    )
