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
GREEN = (126, 200, 140)   # free tier
YELLOW = (220, 180, 90)   # cheap paid tier
MAGENTA = (198, 130, 220)  # Claude Pro tier


def _level_rgb(config: Config, level: str) -> tuple[int, int, int]:
    """Colour a level by cost tier: green free, yellow cheap, magenta Pro."""
    lvl = config.levels.get(level)
    if lvl is None:
        return DIM
    if lvl.framework == "claude":
        return MAGENTA
    if lvl.price_in == 0 and lvl.price_out == 0:
        return GREEN
    return YELLOW


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
        "• /help · /journal · /config · exit",
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


def _attempt_note(level: str, reason: str, stdout: str, *, tail: int = 20) -> str:
    """One escalation-journal entry: the failed level, why, and its output tail.

    The output gives the higher model the actual error text, not just the
    failure category, when the task is escalated.
    """
    note = f"{level}: {reason}"
    lines = [ln for ln in (stdout or "").splitlines() if ln.strip()]
    if lines:
        body = "\n".join("    " + ln for ln in lines[-tail:])
        note += f"\n  вывод (последние строки):\n{body}"
    return note


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
                runner.run_framework(dec, prompt, cfg, steps,
                                     repo_root=repo_root, dry_run=dry_run))
    detect = d.get("detect_failure", lambda res, cfg, root:
                   escalate.detect_failure(res, cfg, root))
    checkpoint = d.get("checkpoint", lambda root: checkpoint_commit(root))
    journal_append = d.get("journal_append", journal.append)

    if not args.no_commit_guard and not args.dry_run:
        checkpoint(repo_root)  # silent safety commit; roll back with /undo

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
        lvl = _color(decision.level, _level_rgb(config, decision.level), bold=True)
        print(f"{_color('→', DIM)} {lvl} · {_color(model, DIM)}  "
              f"{_color('(' + _explain_basis(decision.basis) + ')', DIM)}")

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

        if result.cost_limit_hit:
            print(f"⛔ Превышен потолок стоимости ${config.cost_ceiling_usd} — "
                  "задача прервана (эскалации нет, чтобы не тратить больше).",
                  file=sys.stderr)
            entry = journal.new_entry(args.task, decision.level, decision.basis,
                                      decision.framework, result.model,
                                      "cost-ceiling", 0, run_cost, escalations)
            journal_append(entry, Path(config.journal_path).expanduser())
            return 6

        reason = detect(result, config, repo_root)

        if reason is None:
            entry = journal.new_entry(args.task, decision.level, decision.basis,
                                      decision.framework, result.model,
                                      "success", 0, run_cost, escalations)
            journal_append(entry, Path(config.journal_path).expanduser())
            return 0

        attempts.append(_attempt_note(decision.level, reason, result.stdout))
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


def _print_help(config: Config) -> None:
    print(_color("Команды Relay:", ACCENT, bold=True))
    rows = [
        ("<задача>", "описать задачу — уровень выберется сам (флаги не нужны)"),
        ("/l0 .. /l3 <задача>", "форсировать уровень"),
        ("/dry [on|off]", "режим «показать, не запуская» на всю сессию"),
        ("/steps N", "лимит шагов агента на сессию (/steps off — сброс)"),
        ("/test <cmd>", "прогонять тест после задачи (/test off — выкл)"),
        ("/guard [on|off]", "чекпоинт-коммит перед агентом (по умолч. вкл)"),
        ("/undo", "откатить изменения последней задачи"),
        ("/journal", "журнал решений (уровень, исход, стоимость)"),
        ("/config", "лестница моделей и настройки"),
        ("/clear", "очистить экран"),
        ("/help", "эта справка"),
        ("/exit, exit, Ctrl-D", "выход"),
    ]
    for cmd, desc in rows:
        print(f"  {_color(cmd, ACCENT)}")
        print(f"      {_color(desc, DIM)}")
    print(_color("  Режимы сессии видны в приглашении: [dry steps=10 test] ❯", DIM))
    print(_color("  Уровни: L0/L1 бесплатно · L2 дёшево · L3 Claude (подписка)",
                 DIM))


def _print_config(config: Config, repo_root: Path) -> None:
    print(_color("Лестница моделей:", ACCENT, bold=True))
    for lvl in LADDER:
        L = config.levels[lvl]
        if L.framework == "claude":
            price = "подписка Pro"
        elif L.price_in == 0 and L.price_out == 0:
            price = "бесплатно"
        else:
            price = f"${L.price_in}/${L.price_out} за 1M"
        print(f"  {_color(lvl, ACCENT)} · {L.models[0]} · {L.framework} · {price}")
    print(_color("Настройки:", ACCENT, bold=True))
    print(f"  классификатор: {config.classifier_model}")
    print(f"  лимит шагов: {config.max_steps} · потолок ${config.cost_ceiling_usd}")
    print(f"  окно Pro: {config.pro_window_max_runs} запусков / "
          f"{config.pro_window_hours}ч")
    print(f"  журнал: {config.journal_path}")
    print(f"  папка: {repo_root}")


def _last_checkpoint(repo_root: Path) -> str | None:
    proc = subprocess.run(
        ["git", "log", "--grep=orchestrator: checkpoint", "--format=%H", "-n", "1"],
        cwd=str(repo_root), capture_output=True, text=True)
    sha = proc.stdout.strip()
    return sha or None


def _repl_command(line: str, config: Config, repo_root: Path) -> str | None:
    """Handle a /command. Returns 'exit' to quit, '' if handled, None if not one."""
    cmd = line.split()[0].lower()
    if cmd in ("/exit", "/quit"):
        return "exit"
    if cmd == "/undo":
        sha = _last_checkpoint(repo_root)
        if not sha:
            print(_color("Нет чекпоинта для отката.", DIM))
            return ""
        rollback(repo_root, sha)
        print(_color(f"↩ откатил к чекпоинту {sha[:8]} "
                     "(изменения последней задачи отменены).", GREEN))
        return ""
    if cmd == "/help":
        _print_help(config)
        return ""
    if cmd == "/journal":
        for e in journal.read_all(Path(config.journal_path).expanduser()):
            print(f"{e.timestamp}  {e.level:3}  {e.outcome:12}  "
                  f"${e.cost_usd:.4f}  {e.task}")
        return ""
    if cmd == "/config":
        _print_config(config, repo_root)
        return ""
    if cmd == "/clear":
        print("\x1b[2J\x1b[H", end="")
        print(_banner(repo_root, config))
        return ""
    return None


def _new_session() -> dict:
    return {"dry_run": False, "no_commit_guard": False,
            "max_steps": None, "test_cmd": None}


def _session_command(line: str, session: dict) -> str | None:
    """Set a session-wide default. Returns a status message, or None if not one."""
    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    def onoff(cur: bool) -> bool:
        return True if arg.lower() == "on" else False if arg.lower() == "off" else not cur

    if cmd == "/dry":
        session["dry_run"] = onoff(session["dry_run"])
        return _color(f"режим dry-run: {'вкл' if session['dry_run'] else 'выкл'}", DIM)
    if cmd == "/guard":
        # guard on = checkpoint commit before agents; off = skip it
        session["no_commit_guard"] = (
            False if arg.lower() == "on" else True if arg.lower() == "off"
            else not session["no_commit_guard"])
        on = not session["no_commit_guard"]
        return _color(f"чекпоинт-коммит: {'вкл' if on else 'выкл'}", DIM)
    if cmd == "/steps":
        if arg.lower() in ("", "off", "0"):
            session["max_steps"] = None
            return _color("лимит шагов: по умолчанию", DIM)
        try:
            session["max_steps"] = int(arg)
        except ValueError:
            return _color("Использование: /steps N   (или /steps off)", DIM)
        return _color(f"лимит шагов: {session['max_steps']}", DIM)
    if cmd == "/test":
        if arg.lower() in ("", "off"):
            session["test_cmd"] = None
            return _color("test-cmd: выкл", DIM)
        session["test_cmd"] = arg
        return _color(f"test-cmd: {arg}", DIM)
    return None


def _apply_session(args: ParsedArgs, session: dict) -> ParsedArgs:
    """Fold session defaults into a parsed line (inline flags still win)."""
    if session["dry_run"]:
        args.dry_run = True
    if session["no_commit_guard"]:
        args.no_commit_guard = True
    if args.max_steps is None:
        args.max_steps = session["max_steps"]
    if args.test_cmd is None:
        args.test_cmd = session["test_cmd"]
    return args


def _prompt(session: dict) -> str:
    tags = []
    if session["dry_run"]:
        tags.append("dry")
    if session["no_commit_guard"]:
        tags.append("no-guard")
    if session["max_steps"] is not None:
        tags.append(f"steps={session['max_steps']}")
    if session["test_cmd"]:
        tags.append("test")
    status = _color(f"[{' '.join(tags)}] ", DIM) if tags else ""
    return status + _color("❯ ", bold=True)


def interactive(config: Config, repo_root: Path, *, input_fn=None,
                dispatch=None) -> int:
    """Interactive REPL: read a task per line and route it, until EOF/exit."""
    input_fn = input_fn or input  # resolved at call time so tests can patch it
    dispatch = dispatch or _dispatch
    session = _new_session()
    print(_banner(repo_root, config))
    if not ensure_git_repo(repo_root):
        print(_color(
            "  ⚠ Это не git-репозиторий — агенты здесь не запустятся "
            "(нужен чекпоинт для отката).", ACCENT))
        print(_color(
            "    Перейди в проект: cd ~/твой-проект && relay   "
            "(или добавляй --no-commit-guard к задаче).", DIM))
    while True:
        try:
            line = input_fn(_prompt(session))
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
        # Slash-commands (but not the /l0../l3 level prefixes).
        first = line.split()[0].lower()
        if first.startswith("/") and first not in PREFIXES:
            msg = _session_command(line, session)
            if msg is not None:
                print(msg)
                continue
            result = _repl_command(line, config, repo_root)
            if result == "exit":
                return 0
            if result == "":
                continue
            print(_color(f"Неизвестная команда {first}. /help — список.", DIM),
                  file=sys.stderr)
            continue
        try:
            parsed = _apply_session(parse_args(shlex.split(line)), session)
        except ValueError as exc:
            print(f"Не удалось разобрать строку: {exc}", file=sys.stderr)
            continue
        try:
            dispatch(parsed, config, repo_root)
        except Exception as exc:  # keep the session alive on any per-task error
            print(f"Ошибка: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    # Never crash on a stray surrogate/undecodable char in framework output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    load_env_file()  # pick up OPENROUTER_API_KEY from a .env in the CWD
    config = load_config()
    repo_root = Path.cwd()

    # Bare `relay` (no arguments at all) opens the interactive REPL.
    if not argv:
        return interactive(config, repo_root)

    return _dispatch(args, config, repo_root)
