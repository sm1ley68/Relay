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
    cost_limit_hit: bool = False


class _StreamParser:
    """Stateful parser for a framework's json event stream.

    Feeds text to a writer live, accumulates token/cost usage, and counts
    agent steps — so a caller can stop the run when a step or cost limit is hit.
    """

    def __init__(self, framework: str):
        self.framework = framework
        self.captured: list[str] = []
        self.usage = {"input": 0, "output": 0, "reasoning": 0,
                      "context": 0, "cost": 0.0, "steps": 0}

    @property
    def text(self) -> str:
        return "".join(self.captured)

    def feed(self, raw: str, write) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            evt = json.loads(line)
        except ValueError:
            write(raw if raw.endswith("\n") else raw + "\n")
            self.captured.append(line)
            return
        if self.framework == "opencode":
            self._feed_opencode(evt, write)
        else:
            self._feed_claude(evt, write)

    def _feed_opencode(self, evt: dict, write) -> None:
        etype = evt.get("type")
        part = evt.get("part", {})
        if etype == "text":
            text = part.get("text", "")
            if text:
                write(text)
                self.captured.append(text)
        elif etype == "tool_use":
            tool = part.get("tool", "")
            if tool:
                write(f"\n  ⚙ {tool}\n")
        elif etype == "step_finish":
            tk = part.get("tokens", {}) or {}
            u = self.usage
            u["input"] += tk.get("input", 0) or 0
            u["output"] += tk.get("output", 0) or 0
            u["reasoning"] += tk.get("reasoning", 0) or 0
            u["context"] = max(u["context"], tk.get("total", 0) or 0)
            u["cost"] += part.get("cost", 0) or 0
            u["steps"] += 1

    def _feed_claude(self, evt: dict, write) -> None:
        etype = evt.get("type")
        if etype == "assistant":
            self.usage["steps"] += 1
            for c in evt.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text"):
                    write(c["text"] + "\n")
                    self.captured.append(c["text"])
                elif c.get("type") == "tool_use" and c.get("name"):
                    write(f"\n  ⚙ {c['name']}\n")
        elif etype == "result":
            u = evt.get("usage", {}) or {}
            us = self.usage
            us["input"] += u.get("input_tokens", 0) or 0
            us["output"] += u.get("output_tokens", 0) or 0
            ctx = ((u.get("input_tokens", 0) or 0)
                   + (u.get("cache_creation_input_tokens", 0) or 0)
                   + (u.get("cache_read_input_tokens", 0) or 0))
            us["context"] = max(us["context"], ctx)
            us["cost"] += evt.get("total_cost_usd", 0) or 0


def _consume_opencode_json(lines, write) -> tuple[str, dict]:
    parser = _StreamParser("opencode")
    for line in lines:
        parser.feed(line, write)
    return parser.text, parser.usage


def _consume_claude_json(lines, write) -> tuple[str, dict]:
    parser = _StreamParser("claude")
    for line in lines:
        parser.feed(line, write)
    return parser.text, parser.usage


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


def _run_streaming_json(argv: list[str], framework: str, *, max_steps: int,
                        cost_ceiling: float):
    # Stream a framework's json events: print text live, collect usage, and
    # terminate the process if the step limit or cost ceiling is exceeded.
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    parser = _StreamParser(framework)
    step_limit_hit = cost_limit_hit = False

    def _write(s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    for line in proc.stdout:
        parser.feed(line, _write)
        if max_steps and parser.usage["steps"] > max_steps:
            step_limit_hit = True
            proc.terminate()
            break
        if cost_ceiling and parser.usage["cost"] > cost_ceiling:
            cost_limit_hit = True
            proc.terminate()
            break
    proc.wait()
    return (proc.returncode, parser.text, "", parser.usage,
            step_limit_hit, cost_limit_hit)


_JSON_FRAMEWORKS = {"opencode", "claude"}


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

    # Parse the framework's json stream to surface usage and enforce limits;
    # an injected _runner (tests) always takes the plain path.
    use_json = _runner is None and decision.framework in _JSON_FRAMEWORKS

    last_error: Exception | None = None
    for model in decision.models:
        argv = build_command(template, model, prompt, max_steps)
        try:
            if use_json:
                code, out, err, usage, step_hit, cost_hit = _run_streaming_json(
                    argv, decision.framework, max_steps=max_steps,
                    cost_ceiling=config.cost_ceiling_usd)
            else:
                code, out, err = runner(argv)
                usage, step_hit, cost_hit = None, False, False
        except FileNotFoundError as exc:
            last_error = exc
            continue
        return RunResult(code, out or "", err or "", model,
                         step_limit_hit=step_hit, usage=usage,
                         cost_limit_hit=cost_hit)

    raise FrameworkNotFound(
        f"Не найден бинарь каркаса '{decision.framework}'. "
        f"Установите его или поправьте команду в config.toml. ({last_error})"
    )
