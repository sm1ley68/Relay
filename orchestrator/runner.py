from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

from .config import Config
from .router import RouteDecision


class FrameworkNotFound(Exception):
    pass


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    model: str
    step_limit_hit: bool = False


def build_command(template: str, model: str, prompt: str, steps: int) -> list[str]:
    if model.startswith("-") or prompt.startswith("-"):
        raise ValueError(
            "Значения model/prompt не могут начинаться с '-' "
            "(защита от подмены флагов каркаса)."
        )
    subst = {"{model}": model, "{prompt}": prompt, "{steps}": str(steps)}
    argv = []
    for tok in shlex.split(template):
        argv.append(subst.get(tok, tok))
    return argv


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def run_framework(decision: RouteDecision, prompt: str, config: Config,
                  max_steps: int, *, dry_run: bool = False,
                  _runner=None) -> RunResult:
    template = (config.opencode_cmd if decision.framework == "opencode"
                else config.claude_cmd)
    runner = _runner or _default_runner

    if dry_run:
        model = decision.models[0]
        argv = build_command(template, model, prompt, max_steps)
        print("[dry-run]", " ".join(shlex.quote(a) for a in argv))
        return RunResult(0, "", "", model, False)

    last_error: Exception | None = None
    for model in decision.models:
        argv = build_command(template, model, prompt, max_steps)
        try:
            code, out, err = runner(argv)
        except FileNotFoundError as exc:
            last_error = exc
            continue
        return RunResult(code, out or "", err or "", model, False)

    raise FrameworkNotFound(
        f"Не найден бинарь каркаса '{decision.framework}'. "
        f"Установите его или поправьте команду в config.toml. ({last_error})"
    )
