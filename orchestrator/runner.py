from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from .config import Config
from .i18n import t
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
    timeout_hit: bool = False


def _safe(s: str) -> str:
    """Strip lone surrogates so text can be written to stdout / passed as argv.

    Framework json can carry escaped surrogates (e.g. from truncated multibyte
    tool output); writing them to a strict-utf8 stream raises UnicodeEncodeError.
    """
    return s.encode("utf-8", "replace").decode("utf-8")


def _error_message(err) -> str:
    """Pull a human-readable message out of a framework's error payload.

    Shapes differ per framework and version, so dig for the usual keys and
    fall back to the raw json rather than losing the error entirely.
    """
    if err is None:
        return ""
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict):
            for key in ("message", "error", "detail"):
                if data.get(key):
                    return str(data[key])
        for key in ("message", "detail", "name"):
            if err.get(key):
                return str(err[key])
        return json.dumps(err, ensure_ascii=False)
    return str(err)


class _IdleWatchdog:
    """Kill a process that has gone quiet for ``timeout`` seconds.

    Deliberately idle-based, not a total run cap: a long but healthy task
    keeps streaming output, and killing it would escalate to a pricier level
    and redo work that was already progressing.
    """

    def __init__(self, proc, timeout: float):
        self.proc = proc
        self.timeout = timeout
        self.fired = False
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if self.timeout and self.timeout > 0:
            self._thread = threading.Thread(target=self._watch, daemon=True)
            self._thread.start()
        return self

    def ping(self) -> None:
        self._last = time.monotonic()

    def _watch(self) -> None:
        while not self._stop.wait(0.5):
            if time.monotonic() - self._last >= self.timeout:
                self.fired = True
                try:
                    self.proc.kill()
                except OSError:
                    pass
                return

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)


class _StreamParser:
    """Stateful parser for a framework's json event stream.

    Feeds text to a writer live, accumulates token/cost usage, and counts
    agent steps — so a caller can stop the run when a step or cost limit is hit.
    """

    def __init__(self, framework: str):
        self.framework = framework
        self.captured: list[str] = []
        self.errors: list[str] = []
        # "assistant" bytes only — error text is captured too (the journal and
        # the escalation note need it), but it must not be mistaken for the
        # model having actually produced something.
        self.usage = {"input": 0, "output": 0, "reasoning": 0,
                      "context": 0, "cost": 0.0, "steps": 0, "assistant_chars": 0}

    @property
    def text(self) -> str:
        return "".join(self.captured)

    def _say(self, text: str, write, *, newline: bool = False) -> None:
        write(text + "\n" if newline else text)
        self.captured.append(text)
        self.usage["assistant_chars"] += len(text.strip())

    def _error(self, message: str, write) -> None:
        """Surface a framework error event.

        Errors arrive as ordinary json events, so without this they would be
        parsed and silently dropped — leaving the user staring at a blank
        failure and starving the model-rotation check, which reads this text.
        """
        message = _safe(message.strip())
        if not message:
            return
        self.errors.append(message)
        line = f"  ⚠ {message}\n"
        write(line)
        self.captured.append(line)

    def feed(self, raw: str, write) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            evt = json.loads(line)
        except ValueError:
            safe = _safe(raw if raw.endswith("\n") else raw + "\n")
            write(safe)
            self.captured.append(_safe(line))
            return
        if self.framework == "opencode":
            self._feed_opencode(evt, write)
        else:
            self._feed_claude(evt, write)

    def _feed_opencode(self, evt: dict, write) -> None:
        etype = evt.get("type")
        part = evt.get("part", {})
        if etype == "text":
            text = _safe(part.get("text", ""))
            if text:
                self._say(text, write)
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
        elif etype == "error":
            self._error(_error_message(evt.get("error")), write)

    def _feed_claude(self, evt: dict, write) -> None:
        etype = evt.get("type")
        if etype == "assistant":
            self.usage["steps"] += 1
            for c in evt.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text"):
                    self._say(_safe(c["text"]), write, newline=True)
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
            if evt.get("is_error") or str(evt.get("subtype", "")).startswith("error"):
                self._error(str(evt.get("result")
                                or evt.get("subtype") or "run failed"), write)
        elif etype == "error":
            self._error(_error_message(evt.get("error")), write)


def build_command(template: str, model: str, prompt: str, steps: int) -> list[str]:
    if model.startswith("-") or prompt.startswith("-"):
        raise ValueError(
            t("model/prompt values must not start with '-' "
              "(protection against framework flag smuggling).",
              "Значения model/prompt не могут начинаться с '-' "
              "(защита от подмены флагов каркаса).")
        )
    subst = {"{model}": model, "{prompt}": prompt, "{steps}": str(steps)}
    argv = []
    for tok in shlex.split(template):
        argv.append(subst.get(tok, tok))
    return argv


def _framework_env(cwd) -> dict:
    # Some frameworks read $PWD instead of getcwd(); keep them in sync so the
    # agent operates on the target project regardless of how relay was invoked.
    env = dict(os.environ)
    if cwd is not None:
        env["PWD"] = str(cwd)
    return env


def _run_streaming_json(argv: list[str], framework: str, *, max_steps: int,
                        cost_ceiling: float, timeout: float = 0, cwd=None):
    # Stream a framework's json events: print text live, collect usage, and
    # terminate the process on step limit / cost ceiling / wall-clock timeout.
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            cwd=cwd, env=_framework_env(cwd))
    assert proc.stdout is not None
    parser = _StreamParser(framework)
    step_limit_hit = cost_limit_hit = False

    def _write(s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    with _IdleWatchdog(proc, timeout) as watchdog:
        for line in proc.stdout:
            watchdog.ping()
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
            step_limit_hit, cost_limit_hit, watchdog.fired)


def _run_streaming_plain(argv: list[str], *, timeout: float = 0, cwd=None):
    # Stream any CLI's output live (no token stats), with a wall-clock timeout.
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            cwd=cwd, env=_framework_env(cwd))
    assert proc.stdout is not None
    captured: list[str] = []
    with _IdleWatchdog(proc, timeout) as watchdog:
        for line in proc.stdout:
            watchdog.ping()
            line = _safe(line)
            sys.stdout.write(line)
            sys.stdout.flush()
            captured.append(line)
    proc.wait()
    return proc.returncode, "".join(captured), "", watchdog.fired


# json event format -> parser kind
_JSON_KINDS = {"opencode-json": "opencode", "claude-json": "claude"}
_RATE_LIMIT_BACKOFF = 2.0  # seconds before trying the next model on a 429

_RATE_LIMIT_HINTS = ("rate limit", "rate-limit", "rate limited", "rate-limited",
                     "too many requests", "error 429", "\"code\": 429",
                     "code: 429", "quota exceed")
_UNAVAILABLE_HINTS = ("no endpoints found", "not a valid model", "model not found",
                      "model_not_found", "invalid model", "no allowed providers",
                      "is not available", "unknown model", "no instances available")


def _classify_model_error(output: str) -> str | None:
    """Detect a per-model failure (so we can rotate to the next model)."""
    low = (output or "").lower()
    if any(h in low for h in _RATE_LIMIT_HINTS):
        return "rate-limit"
    if any(h in low for h in _UNAVAILABLE_HINTS):
        return "model-unavailable"
    return None


def _is_model_fault(output: str, exit_code: int, usage: dict | None) -> str | None:
    """Should we rotate to the next model rather than escalate a whole level?

    Beyond the recognised rate-limit / unavailable wordings, a run that failed
    without taking a single step or emitting a byte of *assistant* text never
    got off the ground — that's the model, not the task. Rotating to the
    sibling model is far cheaper than escalating to the next (pricier) level.

    Error text doesn't count as output here: it is exactly what a dead model
    produces, and counting it would make this check never fire.
    """
    named = _classify_model_error(output)
    if named:
        return named
    if exit_code == 0:
        return None
    if usage is None:                      # no telemetry (plain-text framework)
        return "no-output" if not (output or "").strip() else None
    if not usage.get("steps") and not usage.get("assistant_chars"):
        return "no-output"
    return None


def run_framework(decision: RouteDecision, prompt: str, config: Config,
                  max_steps: int, *, repo_root=None, dry_run: bool = False,
                  auto: bool = False, _runner=None) -> RunResult:
    fw = config.frameworks.get(decision.framework)
    if fw is None:
        raise FrameworkNotFound(t(
            f"Framework '{decision.framework}' is not defined in config.toml "
            f"(section [frameworks.{decision.framework}]).",
            f"Каркас '{decision.framework}' не описан в config.toml "
            f"(секция [frameworks.{decision.framework}])."))
    template = fw.cmd
    kind = _JSON_KINDS.get(fw.format)  # None => plain "text" streaming
    auto_flags = list(fw.auto) if auto else []

    if dry_run:
        # Include auto_flags: the preview must be the command that would really
        # run, otherwise --dry-run understates what the agent is allowed to do.
        model = decision.models[0]
        argv = build_command(template, model, prompt, max_steps) + auto_flags
        print("[dry-run]", " ".join(shlex.quote(a) for a in argv))
        return RunResult(0, "", "", model, False)

    cwd = str(repo_root) if repo_root is not None else None

    last_error: Exception | None = None
    models = decision.models
    for i, model in enumerate(models):
        argv = build_command(template, model, prompt, max_steps) + auto_flags
        try:
            if _runner is not None:                       # tests
                code, out, err = _runner(argv)
                usage, step_hit, cost_hit, timeout_hit = None, False, False, False
            elif kind is not None:                        # json framework
                (code, out, err, usage, step_hit, cost_hit,
                 timeout_hit) = _run_streaming_json(
                    argv, kind, max_steps=max_steps,
                    cost_ceiling=config.cost_ceiling_usd,
                    timeout=config.task_timeout_seconds, cwd=cwd)
            else:                                         # any other CLI (text)
                code, out, err, timeout_hit = _run_streaming_plain(
                    argv, timeout=config.task_timeout_seconds, cwd=cwd)
                usage, step_hit, cost_hit = None, False, False
        except FileNotFoundError as exc:
            last_error = exc
            continue

        # Rotate to the next model in the list if THIS model failed (rotation /
        # rate-limit), rather than escalating a whole level. Our own
        # interventions (step/cost/timeout) are not model faults.
        if not (step_hit or cost_hit or timeout_hit) and i < len(models) - 1:
            model_err = _is_model_fault(out, code, usage)
            if model_err:
                nxt = models[i + 1]
                print(t(f"  ↻ model {model} unavailable ({model_err}) → trying {nxt}",
                        f"  ↻ модель {model} недоступна ({model_err}) → пробую {nxt}"),
                      file=sys.stderr)
                if model_err == "rate-limit":
                    time.sleep(_RATE_LIMIT_BACKOFF)
                continue

        return RunResult(code, out or "", err or "", model,
                         step_limit_hit=step_hit, usage=usage,
                         cost_limit_hit=cost_hit, timeout_hit=timeout_hit)

    raise FrameworkNotFound(t(
        f"Framework binary '{decision.framework}' not found. "
        f"Install it or fix the command in config.toml. ({last_error})",
        f"Не найден бинарь каркаса '{decision.framework}'. "
        f"Установите его или поправьте команду в config.toml. ({last_error})"))
