from __future__ import annotations

import json
import shlex
import subprocess
import sys
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
    usage: dict | None = None


def _consume_opencode_json(lines, write) -> tuple[str, dict]:
    """Parse opencode's ``--format json`` event stream.

    Streams assistant text (and compact tool markers) via ``write`` and
    accumulates token/cost usage from ``step_finish`` events. Returns the
    captured assistant text and a usage dict.
    """
    captured: list[str] = []
    usage = {"input": 0, "output": 0, "reasoning": 0, "context": 0, "cost": 0.0}
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            write(raw if raw.endswith("\n") else raw + "\n")
            captured.append(line)
            continue
        etype = evt.get("type")
        part = evt.get("part", {})
        if etype == "text":
            text = part.get("text", "")
            if text:
                write(text)
                captured.append(text)
        elif etype == "tool_use":
            tool = part.get("tool", "")
            if tool:
                write(f"\n  ⚙ {tool}\n")
        elif etype == "step_finish":
            tk = part.get("tokens", {}) or {}
            usage["input"] += tk.get("input", 0) or 0
            usage["output"] += tk.get("output", 0) or 0
            usage["reasoning"] += tk.get("reasoning", 0) or 0
            usage["context"] = max(usage["context"], tk.get("total", 0) or 0)
            usage["cost"] += part.get("cost", 0) or 0
    return "".join(captured), usage


def _consume_claude_json(lines, write) -> tuple[str, dict]:
    """Parse Claude Code's ``--output-format stream-json`` event stream.

    Streams assistant text (and compact tool markers) via ``write`` and reads
    token/cost usage from the final ``result`` event. Returns the captured
    assistant text and a usage dict.
    """
    captured: list[str] = []
    usage = {"input": 0, "output": 0, "reasoning": 0, "context": 0, "cost": 0.0}
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            write(raw if raw.endswith("\n") else raw + "\n")
            captured.append(line)
            continue
        etype = evt.get("type")
        if etype == "assistant":
            for c in evt.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text"):
                    write(c["text"] + "\n")
                    captured.append(c["text"])
                elif c.get("type") == "tool_use" and c.get("name"):
                    write(f"\n  ⚙ {c['name']}\n")
        elif etype == "result":
            u = evt.get("usage", {}) or {}
            usage["input"] += u.get("input_tokens", 0) or 0
            usage["output"] += u.get("output_tokens", 0) or 0
            ctx = ((u.get("input_tokens", 0) or 0)
                   + (u.get("cache_creation_input_tokens", 0) or 0)
                   + (u.get("cache_read_input_tokens", 0) or 0))
            usage["context"] = max(usage["context"], ctx)
            usage["cost"] += evt.get("total_cost_usd", 0) or 0
    return "".join(captured), usage


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
    # Stream the framework's output live to the console while capturing it, so
    # the user sees the agent working (like claude/gemini) and we still keep the
    # text for loop detection. stderr is merged into stdout for a single stream.
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    captured: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    proc.wait()
    return proc.returncode, "".join(captured), ""


def _run_streaming_json(argv: list[str], consume) -> tuple[int, str, str, dict]:
    # Stream a framework's json events: print assistant text live, collect usage.
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None

    def _write(s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    text, usage = consume(proc.stdout, _write)
    proc.wait()
    return proc.returncode, text, "", usage


# json event parser per framework (for token/context/cost surfacing)
_JSON_CONSUMERS = {
    "opencode": _consume_opencode_json,
    "claude": _consume_claude_json,
}


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

    # Parse the framework's json stream to surface token/context usage;
    # an injected _runner (tests) always takes the plain path.
    consume = None if _runner is not None else _JSON_CONSUMERS.get(decision.framework)

    last_error: Exception | None = None
    for model in decision.models:
        argv = build_command(template, model, prompt, max_steps)
        try:
            if consume is not None:
                code, out, err, usage = _run_streaming_json(argv, consume)
            else:
                code, out, err = runner(argv)
                usage = None
        except FileNotFoundError as exc:
            last_error = exc
            continue
        return RunResult(code, out or "", err or "", model, False, usage)

    raise FrameworkNotFound(
        f"Не найден бинарь каркаса '{decision.framework}'. "
        f"Установите его или поправьте команду в config.toml. ({last_error})"
    )
