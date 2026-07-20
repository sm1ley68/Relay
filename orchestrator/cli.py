from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import budget, escalate, journal, router, runner
from .config import Config, LADDER, load_config, load_env_file

PREFIXES = {f"/l{i}": lvl for i, lvl in enumerate(LADDER)}

ACCENT = (215, 138, 126)  # Claude-ish salmon
DIM = (140, 140, 140)


def _color(text: str, rgb=ACCENT, *, bold=False) -> str:
    if not sys.stdout.isatty():
        return text
    r, g, b = rgb
    prefix = ("\x1b[1;" if bold else "\x1b[") + f"38;2;{r};{g};{b}m"
    return f"{prefix}{text}\x1b[0m"


_MASCOT = [
    r"   /\     /\  ",
    r"  {  `---'  } ",
    r"  {  O   O  } ",
    r"  ~~>  V  <~~ ",
    r"   \  \|/  /  ",
    r"    `-----'   ",
]

_RELAY_ART = [
    "██████╗ ███████╗██╗      █████╗ ██╗   ██╗",
    "██╔══██╗██╔════╝██║     ██╔══██╗╚██╗ ██╔╝",
    "██████╔╝█████╗  ██║     ███████║ ╚████╔╝ ",
    "██╔══██╗██╔══╝  ██║     ██╔══██║  ╚██╔╝  ",
    "██║  ██║███████╗███████╗██║  ██║   ██║   ",
    "╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝   ╚═╝   ",
]


def _banner(repo_root: Path, config: Config) -> str:
    cw = 84  # inner content width
    user = (Path.home().name or "there").capitalize()
    cwd = str(repo_root)
    home = str(Path.home())
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]

    def _short(model: str) -> str:
        m = model.removeprefix("openrouter/")
        return m.split("/")[0].split(":")[0]  # provider / bare name

    ladder = " · ".join(f"{lvl} {_short(config.levels[lvl].models[0])}"
                        for lvl in LADDER)

    tips = [
        _color("Подсказки", DIM),
        _color("─────────────────────────────", DIM),
        "• просто задача → авто-выбор уровня",
        f"• /l0 .. /l{len(LADDER) - 1} — форсировать уровень",
        "• --dry-run — показать, не запуская",
        "• journal · exit",
    ]

    # left column (fixed width): mascot, blank, then the big RELAY letters.
    left_w = max(max(len(m) for m in _MASCOT), max(len(a) for a in _RELAY_ART))
    left_col = ([m.ljust(left_w) for m in _MASCOT]
                + [" " * left_w]
                + [a.ljust(left_w) for a in _RELAY_ART])
    # right column: tips aligned next to the RELAY letters.
    right_col = [""] * (len(_MASCOT) + 1) + tips
    rows: list[str] = [""]
    for i, left in enumerate(left_col):
        tip = right_col[i] if i < len(right_col) else ""
        rows.append(f"  {left}   {tip}")
    rows += [
        "",
        f"  Привет, {user}! Relay готов — опиши задачу, модель выберется сама.",
        "  " + _color(ladder, DIM),
        "  " + _color(cwd, DIM),
        "",
    ]

    title = _color("Relay v0.1.0", bold=True)
    # visible length of title ignores ANSI codes for border math
    title_vis = "Relay v0.1.0"
    fill = cw + 2 - (len("╭─  ") + len(title_vis))
    top = _color("╭─ ") + title + _color(" " + "─" * fill + "╮")
    bottom = _color("╰" + "─" * (cw + 2) + "╯")
    bar = _color("│")

    def _visible_len(s: str) -> int:
        import re as _re
        return len(_re.sub(r"\x1b\[[0-9;]*m", "", s))

    lines = [top]
    for row in rows:
        pad = cw - _visible_len(row)
        if pad < 0:  # too long: hard-trim ignoring color (rare)
            row, pad = row[:cw], 0
        lines.append(f"{bar} {row}{' ' * pad} {bar}")
    lines.append(bottom)
    return "\n".join(lines)


@dataclass
class ParsedArgs:
    command: str
    task: str
    explicit_level: str | None
    max_steps: int | None
    test_cmd: str | None
    dry_run: bool
    no_commit_guard: bool


def parse_args(argv: list[str]) -> ParsedArgs:
    command = "run"
    explicit_level = None
    max_steps = None
    test_cmd = None
    dry_run = False
    no_commit_guard = False
    words: list[str] = []

    it = iter(argv)
    for tok in it:
        low = tok.lower()
        if low in PREFIXES:
            explicit_level = PREFIXES[low]
        elif low in ("run", "rollback", "journal") and not words:
            command = low
        elif tok == "--dry-run":
            dry_run = True
        elif tok == "--no-commit-guard":
            no_commit_guard = True
        elif tok == "--max-steps":
            max_steps = int(next(it))
        elif tok == "--test-cmd":
            test_cmd = next(it)
        else:
            words.append(tok)

    return ParsedArgs(command, " ".join(words), explicit_level, max_steps,
                      test_cmd, dry_run, no_commit_guard)


def ensure_git_repo(root: Path) -> bool:
    proc = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                          cwd=str(root), capture_output=True, text=True)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def checkpoint_commit(root: Path, *, _runner=None) -> str:
    def run(argv):
        if _runner is not None:
            return _runner(argv)
        return subprocess.run(argv, cwd=str(root), capture_output=True, text=True)
    add_proc = run(["git", "add", "-A"])
    if add_proc.returncode != 0:
        raise RuntimeError(
            "Не удалось создать чекпоинт-коммит (проверьте git user.name/email)."
        )
    commit_proc = run(["git", "commit", "-m", "orchestrator: checkpoint",
                       "--allow-empty"])
    if commit_proc.returncode != 0:
        raise RuntimeError(
            "Не удалось создать чекпоинт-коммит (проверьте git user.name/email)."
        )
    proc = run(["git", "rev-parse", "HEAD"])
    return proc.stdout.strip()


def rollback(root: Path, sha: str, *, _runner=None) -> None:
    argv = ["git", "reset", "--hard", sha]
    if _runner is not None:
        _runner(argv)
    else:
        subprocess.run(argv, cwd=str(root))


def _fmt_k(n: int) -> str:
    """Compact token count: 9487 -> '9.5K', 512 -> '512'."""
    return f"{n / 1000:.1f}K".replace(".0K", "K") if n >= 1000 else str(n)


def _usage_line(usage: dict) -> str:
    """One-line token/context/cost footer for a completed run."""
    inp = usage.get("input", 0)
    out = usage.get("output", 0)
    reasoning = usage.get("reasoning", 0)
    ctx = usage.get("context", 0)
    cost = usage.get("cost", 0.0)
    parts = [f"⛁ токены: {_fmt_k(inp)} in · {_fmt_k(out)} out"]
    if reasoning:
        parts.append(f"{_fmt_k(reasoning)} reasoning")
    parts.append(f"контекст {_fmt_k(ctx)}")
    parts.append(f"${cost:.4f}")
    return _color("  " + " · ".join(parts), DIM)


def _explain_basis(basis: str) -> str:
    """Human-readable reason for why a level was chosen (for the routing line)."""
    if basis == "explicit":
        return "выбрано вручную"
    if basis == "llm-unavailable":
        return "классификатор недоступен, L2 по умолчанию"
    if basis.startswith("llm:"):
        return f"классификатор LLM: сложность {basis.split(':', 1)[1]}/5"
    if basis.startswith("heuristic:"):
        reason = basis.split(":", 1)[1]
        if reason == "failed-low":
            return "эвристика: провалилось на нижнем уровне"
        if reason == "up-keyword":
            return "эвристика: ключевые слова «вверх»"
        if reason == "down-keyword":
            return "эвристика: ключевые слова «вниз»"
        if reason.startswith("up-files:") or reason.startswith("files:"):
            return f"эвристика: затрагивает файлов — {reason.split(':', 1)[1]}"
        return f"эвристика: {reason}"
    return basis


def orchestrate(args: ParsedArgs, config: Config, repo_root: Path, *,
                deps=None) -> int:
    d = deps or {}
    classify = d.get("classify", router.classify)
    run = d.get("run", lambda dec, prompt, cfg, steps, dry_run:
                runner.run_framework(dec, prompt, cfg, steps, dry_run=dry_run))
    detect = d.get("detect_failure", lambda res, cfg, root:
                   escalate.detect_failure(res, cfg, root))
    checkpoint = d.get("checkpoint", lambda root: checkpoint_commit(root))
    journal_append = d.get("journal_append", journal.append)

    checkpoint_sha = None
    if not args.no_commit_guard and not args.dry_run:
        checkpoint_sha = checkpoint(repo_root)
        if checkpoint_sha:
            print(f"Чекпоинт: {checkpoint_sha} "
                  f"(откат: git reset --hard {checkpoint_sha})", file=sys.stderr)

    max_steps = args.max_steps or config.max_steps
    prompt = args.task
    level = args.explicit_level
    attempts: list[str] = []
    escalations: list[str] = []
    pro = budget.ProWindow(Path(config.budget_path).expanduser(),
                           config.pro_window_hours, config.pro_window_max_runs)

    while True:
        decision = classify(args.task, config, explicit_level=level,
                            repo_root=repo_root,
                            already_failed=bool(attempts))

        model = decision.models[0] if decision.models else "?"
        print(f"→ {decision.level} · {model}  ({_explain_basis(decision.basis)})")

        on_claude = decision.framework == "claude"
        if on_claude and not args.dry_run and not pro.can_run():
            print("Окно Claude Pro на исходе — поставьте задачу в очередь "
                  "или подождите сброса лимита.", file=sys.stderr)
            entry = journal.new_entry(args.task, decision.level, decision.basis,
                                      decision.framework, "", "pro-exhausted",
                                      0, 0.0, escalations)
            journal_append(entry, Path(config.journal_path).expanduser())
            return 2

        if on_claude and not args.dry_run:
            pro.record_run()

        result = run(decision, prompt, config, max_steps, args.dry_run)

        if args.dry_run:
            return 0

        run_cost = 0.0
        if result.usage:
            run_cost = float(result.usage.get("cost", 0.0))
            if result.stdout and not result.stdout.endswith("\n"):
                print()  # put the footer on its own line
            print(_usage_line(result.usage))

        reason = detect(result, config, repo_root)

        if reason is None:
            entry = journal.new_entry(args.task, decision.level, decision.basis,
                                      decision.framework, result.model,
                                      "success", 0, run_cost, escalations)
            journal_append(entry, Path(config.journal_path).expanduser())
            return 0

        attempts.append(f"{decision.level}: {reason}")
        nxt = escalate.next_level(decision.level)
        if nxt is None:
            entry = journal.new_entry(args.task, decision.level, decision.basis,
                                      decision.framework, result.model,
                                      f"failed:{reason}", 0, 0.0, escalations)
            journal_append(entry, Path(config.journal_path).expanduser())
            print(f"Провал на верхнем уровне ({decision.level}): {reason}",
                  file=sys.stderr)
            return 1

        escalations.append(f"{decision.level}->{nxt}")
        prompt = escalate.build_escalation_prompt(args.task, attempts, repo_root)
        level = nxt


def _dispatch(args: ParsedArgs, config: Config, repo_root: Path) -> int:
    """Handle one parsed invocation: subcommands, guards, orchestration.

    Shared by the one-shot CLI and the interactive REPL.
    """
    if args.test_cmd is not None:
        import dataclasses
        config = dataclasses.replace(config, test_cmd=args.test_cmd)

    if args.command == "journal":
        for e in journal.read_all(Path(config.journal_path).expanduser()):
            print(f"{e.timestamp}  {e.level:3}  {e.outcome:12}  {e.task}")
        return 0

    if args.command == "rollback":
        print("Откат: git reset --hard <checkpoint>. "
              "Последний чекпоинт см. в `git log`.", file=sys.stderr)
        return 0

    if not ensure_git_repo(repo_root) and not args.no_commit_guard:
        print("Не git-репозиторий — агент не запущен (нужен чекпоинт для отката). "
              "Перейди в проект (cd ~/твой-проект), сделай `git init`, "
              "или добавь --no-commit-guard.", file=sys.stderr)
        return 3

    if not args.task:
        print("Пустая задача. Пример: relay /l2 почини авторизацию",
              file=sys.stderr)
        return 3

    try:
        return orchestrate(args, config, repo_root)
    except runner.FrameworkNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 5


def interactive(config: Config, repo_root: Path, *, input_fn=None,
                dispatch=None) -> int:
    """Interactive REPL: read a task per line and route it, until EOF/exit."""
    input_fn = input_fn or input  # resolved at call time so tests can patch it
    dispatch = dispatch or _dispatch
    print(_banner(repo_root, config))
    if not ensure_git_repo(repo_root):
        print(_color(
            "  ⚠ Это не git-репозиторий — агенты здесь не запустятся "
            "(нужен чекпоинт для отката).", ACCENT))
        print(_color(
            "    Перейди в проект: cd ~/твой-проект && relay   "
            "(или добавляй --no-commit-guard к задаче).", DIM))
    prompt = _color("❯ ", bold=True)
    while True:
        try:
            line = input_fn(prompt)
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("^C")
            continue
        line = line.strip()
        if not line:
            continue
        if line in ("exit", "quit", ":q"):
            return 0
        try:
            parsed = parse_args(shlex.split(line))
        except ValueError as exc:
            print(f"Не удалось разобрать строку: {exc}", file=sys.stderr)
            continue
        try:
            dispatch(parsed, config, repo_root)
        except Exception as exc:  # keep the session alive on any per-task error
            print(f"Ошибка: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    load_env_file()  # pick up OPENROUTER_API_KEY from a .env in the CWD
    config = load_config()
    repo_root = Path.cwd()

    # Bare `relay` (no arguments at all) opens the interactive REPL.
    if not argv:
        return interactive(config, repo_root)

    return _dispatch(args, config, repo_root)
