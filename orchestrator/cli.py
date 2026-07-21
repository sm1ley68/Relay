from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import budget, escalate, journal, router, runner
from .config import Config, LADDER, load_config, load_env_file
from .i18n import t, set_lang, current_lang

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
        _color(t("Tips", "Подсказки"), DIM),
        _color("─────────────────────────────", DIM),
        t("• just type a task → auto-picked level",
          "• просто задача → авто-выбор уровня"),
        t(f"• /l0 .. /l{len(LADDER) - 1} — force a level",
          f"• /l0 .. /l{len(LADDER) - 1} — форсировать уровень"),
        t("• --dry-run — show, don't run", "• --dry-run — показать, не запуская"),
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
        "  " + t(f"Hi, {user}! Relay is ready — describe a task, it picks the model.",
                 f"Привет, {user}! Relay готов — опиши задачу, модель выберется сама."),
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
    auto: bool = False


SUBCOMMANDS = ("run", "rollback", "journal", "stats", "doctor", "models", "init")

_CODEX_CONFIG = '''# Relay ladder for a Codex (ChatGPT) user — no OpenRouter.
max_steps = 40
task_timeout_seconds = 600
cost_ceiling_usd = 0.50
pro_window_hours = 5.0
pro_window_max_runs = 40
classifier_model = ""
journal_path = "~/.orchestrator/journal.jsonl"
budget_path = "~/.orchestrator/budget.json"
test_cmd = ""

[frameworks.codex]
cmd = 'codex exec "{prompt}"'
format = "text"
# Safe default: no sandbox bypass. /auto then relies on Codex's own approval flow.
# Advanced (understand the risk): add "--full-auto" (sandboxed) or, only if you
# really mean it, "--dangerously-bypass-approvals-and-sandbox".
auto = []

[levels.L0]
models = ["codex"]
price_in = 0.0
price_out = 0.0
framework = "codex"
metered = true

[levels.L1]
models = ["codex"]
price_in = 0.0
price_out = 0.0
framework = "codex"
metered = true

[levels.L2]
models = ["codex"]
price_in = 0.0
price_out = 0.0
framework = "codex"
metered = true

[levels.L3]
models = ["codex"]
price_in = 0.0
price_out = 0.0
framework = "codex"
metered = true
'''


def parse_args(argv: list[str]) -> ParsedArgs:
    command = "run"
    explicit_level = None
    max_steps = None
    test_cmd = None
    dry_run = False
    no_commit_guard = False
    auto = False
    words: list[str] = []

    it = iter(argv)
    for tok in it:
        low = tok.lower()
        if low in PREFIXES:
            explicit_level = PREFIXES[low]
        elif low in SUBCOMMANDS and not words:
            command = low
        elif tok == "--dry-run":
            dry_run = True
        elif tok == "--no-commit-guard":
            no_commit_guard = True
        elif tok == "--auto":
            auto = True
        elif tok == "--max-steps":
            max_steps = int(next(it))
        elif tok == "--test-cmd":
            test_cmd = next(it)
        else:
            words.append(tok)

    return ParsedArgs(command, " ".join(words), explicit_level, max_steps,
                      test_cmd, dry_run, no_commit_guard, auto)


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
            t("Failed to create the checkpoint commit "
              "(check git user.name/email).",
              "Не удалось создать чекпоинт-коммит (проверьте git user.name/email).")
        )
    commit_proc = run(["git", "commit", "-m", "orchestrator: checkpoint",
                       "--allow-empty"])
    if commit_proc.returncode != 0:
        raise RuntimeError(
            t("Failed to create the checkpoint commit "
              "(check git user.name/email).",
              "Не удалось создать чекпоинт-коммит (проверьте git user.name/email).")
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
    parts = [t(f"⛁ tokens: {_fmt_k(inp)} in · {_fmt_k(out)} out",
               f"⛁ токены: {_fmt_k(inp)} in · {_fmt_k(out)} out")]
    if reasoning:
        parts.append(f"{_fmt_k(reasoning)} reasoning")
    parts.append(t(f"context {_fmt_k(ctx)}", f"контекст {_fmt_k(ctx)}"))
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
        note += "\n  " + t("output (last lines):", "вывод (последние строки):") \
            + f"\n{body}"
    return note


def _explain_basis(basis: str) -> str:
    """Human-readable reason for why a level was chosen (for the routing line)."""
    if basis == "explicit":
        return t("forced", "выбрано вручную")
    if basis == "llm-unavailable":
        return t("classifier unavailable, default L2",
                 "классификатор недоступен, L2 по умолчанию")
    if basis == "no-classifier":
        return t("no classifier (no key), default L2",
                 "без классификатора (нет ключа), L2 по умолчанию")
    if basis.startswith("llm:"):
        n = basis.split(":", 1)[1]
        return t(f"LLM classifier: difficulty {n}/5",
                 f"классификатор LLM: сложность {n}/5")
    if basis.startswith("heuristic:"):
        reason = basis.split(":", 1)[1]
        if reason == "failed-low":
            return t("heuristic: failed at a lower level",
                     "эвристика: провалилось на нижнем уровне")
        if reason == "up-keyword":
            return t("heuristic: 'up' keywords", "эвристика: ключевые слова «вверх»")
        if reason == "down-keyword":
            return t("heuristic: 'down' keywords", "эвристика: ключевые слова «вниз»")
        if reason.startswith("up-files:") or reason.startswith("files:"):
            n = reason.split(":", 1)[1]
            return t(f"heuristic: touches {n} files",
                     f"эвристика: затрагивает файлов — {n}")
        return t(f"heuristic: {reason}", f"эвристика: {reason}")
    return basis


def orchestrate(args: ParsedArgs, config: Config, repo_root: Path, *,
                deps=None) -> int:
    d = deps or {}
    classify = d.get("classify", router.classify)
    run = d.get("run", lambda dec, prompt, cfg, steps, dry_run:
                runner.run_framework(dec, prompt, cfg, steps,
                                     repo_root=repo_root, dry_run=dry_run,
                                     auto=args.auto))
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

        metered = config.levels[decision.level].metered
        if metered and not args.dry_run and not pro.can_run():
            print(t(f"Subscription window ({decision.framework}) is running out "
                    "— queue the task or wait for the limit to reset.",
                    f"Окно подписки ({decision.framework}) на исходе — поставьте "
                    "задачу в очередь или подождите сброса лимита."),
                  file=sys.stderr)
            entry = journal.new_entry(args.task, decision.level, decision.basis,
                                      decision.framework, "", "pro-exhausted",
                                      0, 0.0, escalations)
            journal_append(entry, Path(config.journal_path).expanduser())
            return 2

        if metered and not args.dry_run:
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
            print(t(f"⛔ Cost ceiling ${config.cost_ceiling_usd} exceeded — task "
                    "stopped (no escalation, to avoid spending more).",
                    f"⛔ Превышен потолок стоимости ${config.cost_ceiling_usd} — "
                    "задача прервана (эскалации нет, чтобы не тратить больше)."),
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
            print(t(f"Failed at the top level ({decision.level}): {reason}",
                    f"Провал на верхнем уровне ({decision.level}): {reason}"),
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

    if args.command == "stats":
        _print_stats(config)
        return 0

    if args.command == "doctor":
        return _doctor(config)

    if args.command == "models":
        return _list_models(config)

    if args.command == "init":
        return _init_wizard()

    if args.command == "rollback":
        print(t("Rollback: git reset --hard <checkpoint>. "
                "See the last checkpoint in `git log` (or use /undo in the REPL).",
                "Откат: git reset --hard <checkpoint>. "
                "Последний чекпоинт см. в `git log`."), file=sys.stderr)
        return 0

    if not ensure_git_repo(repo_root) and not args.no_commit_guard:
        print(t("Not a git repo — agent not run (a checkpoint is needed for undo). "
                "cd into a project, run `git init`, or add --no-commit-guard.",
                "Не git-репозиторий — агент не запущен (нужен чекпоинт для отката). "
                "Перейди в проект (cd ~/твой-проект), сделай `git init`, "
                "или добавь --no-commit-guard."), file=sys.stderr)
        return 3

    if not args.task:
        print(t("Empty task. Example: relay /l2 fix the auth check",
                "Пустая задача. Пример: relay /l2 почини авторизацию"),
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
    print(_color(t("Relay commands:", "Команды Relay:"), ACCENT, bold=True))
    rows = [
        ("<task>", t("describe a task — level is auto-picked (no flags needed)",
                     "описать задачу — уровень выберется сам (флаги не нужны)")),
        ("/l0 .. /l3 <task>", t("force a level", "форсировать уровень")),
        ("/dry [on|off]", t("show-don't-run mode for the session",
                            "режим «показать, не запуская» на всю сессию")),
        ("/auto [on|off]", t("agent edits files without asking permission",
                             "агент правит файлы без запроса разрешений")),
        ("/steps N", t("cap agent steps for the session (/steps off — reset)",
                       "лимит шагов агента на сессию (/steps off — сброс)")),
        ("/test <cmd>", t("run a test after each task (/test off — disable)",
                          "прогонять тест после задачи (/test off — выкл)")),
        ("/guard [on|off]", t("checkpoint commit before the agent (default on)",
                              "чекпоинт-коммит перед агентом (по умолч. вкл)")),
        ("/undo", t("roll back the last task's changes",
                    "откатить изменения последней задачи")),
        ("/journal", t("decision log (level, outcome, cost)",
                       "журнал решений (уровень, исход, стоимость)")),
        ("/stats", t("analytics: tasks per level, cost, escalations",
                     "аналитика: задачи по уровням, стоимость, эскалации")),
        ("/config", t("model ladder and settings", "лестница моделей и настройки")),
        ("/clear", t("clear the screen", "очистить экран")),
        ("/lang [en|ru]", t("switch UI language", "переключить язык интерфейса")),
        ("/help", t("this help", "эта справка")),
        ("/exit, exit, Ctrl-D", t("quit", "выход")),
    ]
    for cmd, desc in rows:
        print(f"  {_color(cmd, ACCENT)}")
        print(f"      {_color(desc, DIM)}")
    print(_color(t("  Session modes show in the prompt: [dry steps=10 test] ❯",
                   "  Режимы сессии видны в приглашении: [dry steps=10 test] ❯"), DIM))


def _print_config(config: Config, repo_root: Path) -> None:
    print(_color(t("Model ladder:", "Лестница моделей:"), ACCENT, bold=True))
    for lvl in LADDER:
        L = config.levels[lvl]
        if L.framework == "claude":
            price = t("Pro subscription", "подписка Pro")
        elif L.price_in == 0 and L.price_out == 0:
            price = t("free", "бесплатно")
        else:
            price = f"${L.price_in}/${L.price_out} " + t("per 1M", "за 1M")
        print(f"  {_color(lvl, ACCENT)} · {L.models[0]} · {L.framework} · {price}")
    print(_color(t("Frameworks:", "Каркасы:"), ACCENT, bold=True))
    for name, fw in config.frameworks.items():
        print(f"  {name} · {fw.format} · {fw.cmd}")
    print(_color(t("Settings:", "Настройки:"), ACCENT, bold=True))
    print("  " + t("classifier", "классификатор") + f": {config.classifier_model}")
    print("  " + t(
        f"step limit: {config.max_steps} · timeout {config.task_timeout_seconds}s"
        f" · ceiling ${config.cost_ceiling_usd}",
        f"лимит шагов: {config.max_steps} · таймаут {config.task_timeout_seconds}с"
        f" · потолок ${config.cost_ceiling_usd}"))
    print("  " + t(
        f"subscription window: {config.pro_window_max_runs} runs / "
        f"{config.pro_window_hours}h",
        f"окно подписки: {config.pro_window_max_runs} запусков / "
        f"{config.pro_window_hours}ч"))
    print("  " + t("config", "конфиг") +
          f": {config.active_path or t('(built-in)', '(встроенный)')}")
    print("  " + t("journal", "журнал") + f": {config.journal_path}")
    print("  " + t("dir", "папка") + f": {repo_root}")


def _last_checkpoint(repo_root: Path) -> str | None:
    proc = subprocess.run(
        ["git", "log", "--grep=orchestrator: checkpoint", "--format=%H", "-n", "1"],
        cwd=str(repo_root), capture_output=True, text=True)
    sha = proc.stdout.strip()
    return sha or None


def _print_stats(config: Config) -> None:
    entries = journal.read_all(Path(config.journal_path).expanduser())
    if not entries:
        print(_color(t("Journal is empty — no tasks yet.",
                       "Журнал пуст — ещё не было задач."), DIM))
        return
    total = len(entries)
    total_cost = sum(e.cost_usd for e in entries)
    escalated = sum(1 for e in entries if e.escalations)
    by_level: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    for e in entries:
        by_level[e.level] = by_level.get(e.level, 0) + 1
        key = e.outcome.split(":")[0]
        by_outcome[key] = by_outcome.get(key, 0) + 1

    print(_color(t(
        f"Tasks: {total} · total ${total_cost:.4f} · "
        f"avg ${total_cost / total:.4f} · "
        f"escalations {escalated} ({escalated * 100 // total}%)",
        f"Задач: {total} · суммарно ${total_cost:.4f} · "
        f"в среднем ${total_cost / total:.4f} · "
        f"эскалаций {escalated} ({escalated * 100 // total}%)"),
        ACCENT, bold=True))
    print(_color(t("By level:", "По уровням:"), DIM))
    for lvl in LADDER:
        n = by_level.get(lvl, 0)
        if n:
            bar = "█" * min(30, n)
            print(f"  {_color(lvl, _level_rgb(config, lvl))} {n:>4}  {bar}")
    print(_color(t("Outcomes:", "Исходы:"), DIM))
    for outcome, n in sorted(by_outcome.items(), key=lambda x: -x[1]):
        print(f"  {outcome:14} {n}")


def _doctor(config: Config) -> int:
    """Check the environment for the ACTIVE config and report what's missing."""
    import shutil
    ok = _color("✓", GREEN)
    bad = _color("✗", (220, 100, 100))
    print(_color(t("Relay doctor — environment check:",
                   "Relay doctor — проверка окружения:"), ACCENT, bold=True))
    print(_color("  " + t("config", "конфиг") +
                 f": {config.active_path or t('(built-in)', '(встроенный)')}", DIM))
    problems = 0

    py_ok = sys.version_info >= (3, 13)
    print(f"  {ok if py_ok else bad} Python {sys.version_info.major}."
          f"{sys.version_info.minor}" +
          ("" if py_ok else t("  (need 3.13+)", "  (нужен 3.13+)")))
    problems += not py_ok

    print(f"  {ok if shutil.which('git') else bad} git")
    problems += not shutil.which("git")

    # Only the CLIs actually used by this ladder.
    used = {config.levels[lv].framework for lv in config.levels}
    for name in sorted(used):
        fw = config.frameworks.get(name)
        binary = shlex.split(fw.cmd)[0] if fw else name
        found = shutil.which(binary)
        hint = {"opencode": "npm i -g opencode-ai",
                "claude": t("install Claude Code and log in",
                            "поставь Claude Code и залогинься"),
                "codex": t("install Codex CLI and `codex login`",
                           "поставь Codex CLI и `codex login`")}.get(
                    name, t("install it", "поставь его"))
        print(f"  {ok if found else bad} {binary}" +
              (f"  {found}" if found
               else "  — " + t("not found", "не найден") + f": {hint}"))
        problems += not found

    # OpenRouter key only if the ladder or classifier actually needs it.
    needs_or = bool(config.classifier_model) or any(
        m.startswith("openrouter/") for lv in config.levels.values()
        for m in lv.models)
    if needs_or:
        key = bool(os.environ.get("OPENROUTER_API_KEY"))
        print(f"  {ok if key else bad} OPENROUTER_API_KEY" +
              ("" if key else t("  — set it in ~/.orchestrator/.env",
                                "  — задай в ~/.orchestrator/.env")))
        problems += not key

    if problems:
        print(_color(t(f"\nMissing {problems} — see the hints above.",
                       f"\nНе хватает {problems} — см. подсказки выше."), DIM))
    else:
        print(_color(t("\nAll set. Run `relay` in a project.",
                       "\nВсё на месте. Запускай `relay` в проекте."), GREEN))
    return 0 if problems == 0 else 1


def _write_secret(path: Path, content: str) -> None:
    """Write a secret file atomically with 0o600 (no world/group read window)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)


def _init_wizard(*, input_fn=None, getpass_fn=None) -> int:
    """Interactive setup: pick a provider, save the key/config, run doctor."""
    input_fn = input_fn or input
    if getpass_fn is None:
        import getpass as _gp
        getpass_fn = _gp.getpass
    home = Path.home() / ".orchestrator"
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)  # keep the secrets dir private
    except OSError:
        pass

    print(_color(t("Relay init — setup", "Relay init — настройка"), ACCENT, bold=True))
    print("  1) OpenRouter — " + t("cheap models (opencode) + Claude on top",
                                    "дешёвые модели (opencode) + Claude сверху"))
    print("  2) Codex (ChatGPT) — " + t("no OpenRouter", "без OpenRouter"))
    try:
        choice = input_fn(t("Provider [1/2]: ", "Провайдер [1/2]: ")).strip()
    except EOFError:
        print(t("Non-interactive. Set the key in ~/.orchestrator/.env manually.",
                "Неинтерактивный режим. Задай ключ в ~/.orchestrator/.env вручную."),
              file=sys.stderr)
        return 1

    if choice == "2":
        (home / "config.toml").write_text(_CODEX_CONFIG)
        print(_color(t("✓ Wrote a codex ladder to ~/.orchestrator/config.toml",
                       "✓ Записал codex-лестницу в ~/.orchestrator/config.toml"), GREEN))
        print(_color(t("  Don't forget to log in: codex login",
                       "  Не забудь залогиниться: codex login"), DIM))
    else:
        try:
            key = getpass_fn("OpenRouter API key (sk-or-...): ").strip()
        except EOFError:
            key = ""
        if key:
            _write_secret(home / ".env", f'OPENROUTER_API_KEY="{key}"\n')
            print(_color(t("✓ Key saved to ~/.orchestrator/.env (0600)",
                           "✓ Ключ записан в ~/.orchestrator/.env (0600)"), GREEN))
        print(_color(t("  Ladder: L0/L1 free · L2 DeepSeek · L3 Claude "
                       "(L3 needs `claude` login)",
                       "  Лестница: L0/L1 free · L2 DeepSeek · L3 Claude "
                       "(для L3 нужен `claude` login)"), DIM))
        # a stale codex override would shadow the default ladder
        override = home / "config.toml"
        if override.exists():
            print(_color(t(f"  ⚠ {override} exists — it overrides the default. "
                           "Delete it for the standard ladder.",
                           f"  ⚠ Есть {override} — он переопределяет дефолт. "
                           "Удали его для стандартной лестницы."), DIM))

    print()
    return _doctor(load_config())


def _list_models(config: Config) -> int:
    """List current free / cheap OpenRouter models — candidates for the ladder."""
    import urllib.request
    import json as _json
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print(t("OPENROUTER_API_KEY (in ~/.orchestrator/.env) is required to list "
                "OpenRouter models.",
                "Нужен OPENROUTER_API_KEY (в ~/.orchestrator/.env), чтобы получить "
                "список моделей OpenRouter."), file=sys.stderr)
        return 1
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())
    except Exception as exc:
        print(t(f"Failed to fetch models: {exc}",
                f"Не удалось получить модели: {exc}"), file=sys.stderr)
        return 1

    free, cheap = [], []
    for m in data.get("data", []):
        p = m.get("pricing", {})
        pin = float(p.get("prompt", 0) or 0) * 1e6
        pout = float(p.get("completion", 0) or 0) * 1e6
        ctx = m.get("context_length", 0) or 0
        if pin == 0 and pout == 0:
            free.append((ctx, m["id"]))
        elif pin <= 1.0:
            cheap.append((pin, pout, m["id"]))
    free.sort(reverse=True)
    cheap.sort()

    print(_color(t(f"Free models ({len(free)}) — for L0/L1:",
                   f"Бесплатные модели ({len(free)}) — для L0/L1:"), ACCENT, bold=True))
    for ctx, ident in free:
        print(f"  openrouter/{ident}   ctx={ctx}")
    print(_color(t("\nCheap (≤ $1/1M in) — for L2:",
                   "\nДешёвые (≤ $1/1M вход) — для L2:"), ACCENT, bold=True))
    for pin, pout, ident in cheap[:15]:
        print(f"  openrouter/{ident}   ${pin:.2f}/${pout:.2f}")
    print(_color(t("\nPut the ones you want in models=[...] in "
                   "~/.orchestrator/config.toml (with the openrouter/ prefix). "
                   "Relay rotates the list on a 429.",
                   "\nВставь нужные в models=[...] в ~/.orchestrator/config.toml "
                   "(с префиксом openrouter/). Relay сам ротирует список при 429."),
                 DIM))
    return 0


def _repl_command(line: str, config: Config, repo_root: Path) -> str | None:
    """Handle a /command. Returns 'exit' to quit, '' if handled, None if not one."""
    cmd = line.split()[0].lower()
    if cmd in ("/exit", "/quit"):
        return "exit"
    if cmd == "/lang":
        arg = line.split()[1].lower() if len(line.split()) > 1 else ""
        set_lang(arg or ("ru" if current_lang() == "en" else "en"))
        print(_color(t(f"language: {current_lang()}", f"язык: {current_lang()}"), DIM))
        return ""
    if cmd == "/undo":
        sha = _last_checkpoint(repo_root)
        if not sha:
            print(_color(t("No checkpoint to roll back to.",
                           "Нет чекпоинта для отката."), DIM))
            return ""
        rollback(repo_root, sha)
        print(_color(t(f"↩ rolled back to checkpoint {sha[:8]} "
                       "(last task's changes undone).",
                       f"↩ откатил к чекпоинту {sha[:8]} "
                       "(изменения последней задачи отменены)."), GREEN))
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
    if cmd == "/stats":
        _print_stats(config)
        return ""
    if cmd == "/clear":
        print("\x1b[2J\x1b[H", end="")
        print(_banner(repo_root, config))
        return ""
    return None


def _new_session() -> dict:
    return {"dry_run": False, "no_commit_guard": False,
            "max_steps": None, "test_cmd": None, "auto": False}


def _session_command(line: str, session: dict) -> str | None:
    """Set a session-wide default. Returns a status message, or None if not one."""
    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    def onoff(cur: bool) -> bool:
        return True if arg.lower() == "on" else False if arg.lower() == "off" else not cur

    def _on(b):
        return t("on", "вкл") if b else t("off", "выкл")

    if cmd == "/dry":
        session["dry_run"] = onoff(session["dry_run"])
        return _color(t("dry-run mode: ", "режим dry-run: ") + _on(session["dry_run"]),
                      DIM)
    if cmd == "/auto":
        session["auto"] = onoff(session["auto"])
        return _color(t("auto-approve edits: ", "авто-одобрение правок: ")
                      + _on(session["auto"])
                      + (t(" (agent edits without asking)",
                           " (агент правит без запроса)") if session["auto"] else ""),
                      DIM)
    if cmd == "/guard":
        # guard on = checkpoint commit before agents; off = skip it
        session["no_commit_guard"] = (
            False if arg.lower() == "on" else True if arg.lower() == "off"
            else not session["no_commit_guard"])
        return _color(t("checkpoint commit: ", "чекпоинт-коммит: ")
                      + _on(not session["no_commit_guard"]), DIM)
    if cmd == "/steps":
        if arg.lower() in ("", "off", "0"):
            session["max_steps"] = None
            return _color(t("step limit: default", "лимит шагов: по умолчанию"), DIM)
        try:
            session["max_steps"] = int(arg)
        except ValueError:
            return _color(t("Usage: /steps N   (or /steps off)",
                            "Использование: /steps N   (или /steps off)"), DIM)
        return _color(t("step limit: ", "лимит шагов: ") + str(session["max_steps"]),
                      DIM)
    if cmd == "/test":
        if arg.lower() in ("", "off"):
            session["test_cmd"] = None
            return _color("test-cmd: " + _on(False), DIM)
        session["test_cmd"] = arg
        return _color(f"test-cmd: {arg}", DIM)
    return None


def _apply_session(args: ParsedArgs, session: dict) -> ParsedArgs:
    """Fold session defaults into a parsed line (inline flags still win)."""
    if session["dry_run"]:
        args.dry_run = True
    if session["no_commit_guard"]:
        args.no_commit_guard = True
    if session["auto"]:
        args.auto = True
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
    if session["auto"]:
        tags.append("auto")
    status = _color(f"[{' '.join(tags)}] ", DIM) if tags else ""
    return status + _color("❯ ", bold=True)


_REPL_COMMANDS = ["/help", "/journal", "/config", "/stats", "/clear", "/undo",
                  "/dry", "/auto", "/steps", "/test", "/guard", "/lang", "/exit",
                  "/l0", "/l1", "/l2", "/l3"]


def _setup_readline() -> None:
    """Enable ↑/↓ history, Ctrl-R search, line editing and tab-completion."""
    try:
        import readline
    except ImportError:
        return
    histfile = Path.home() / ".orchestrator" / "history"
    histfile.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(histfile)
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(2000)
    import atexit
    atexit.register(lambda: _save_history(readline, histfile))

    def completer(text, state):
        opts = [c + " " for c in _REPL_COMMANDS if c.startswith(text)]
        return opts[state] if state < len(opts) else None
    readline.set_completer(completer)
    readline.parse_and_bind("tab: complete")


def _save_history(readline, histfile: Path) -> None:
    try:
        readline.write_history_file(histfile)
    except OSError:
        pass


def interactive(config: Config, repo_root: Path, *, input_fn=None,
                dispatch=None) -> int:
    """Interactive REPL: read a task per line and route it, until EOF/exit."""
    input_fn = input_fn or input  # resolved at call time so tests can patch it
    dispatch = dispatch or _dispatch
    session = _new_session()
    if input_fn is input:  # only touch readline for a real interactive session
        _setup_readline()
    print(_banner(repo_root, config))
    if not ensure_git_repo(repo_root):
        print(_color(t(
            "  ⚠ Not a git repo — agents won't run here (a checkpoint is needed).",
            "  ⚠ Это не git-репозиторий — агенты здесь не запустятся "
            "(нужен чекпоинт для отката)."), ACCENT))
        print(_color(t(
            "    cd into a project: cd ~/your-project && relay   "
            "(or add --no-commit-guard to a task).",
            "    Перейди в проект: cd ~/твой-проект && relay   "
            "(или добавляй --no-commit-guard к задаче)."), DIM))
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
            print(_color(t(f"Unknown command {first}. /help for the list.",
                           f"Неизвестная команда {first}. /help — список."), DIM),
                  file=sys.stderr)
            continue
        try:
            parsed = _apply_session(parse_args(shlex.split(line)), session)
        except ValueError as exc:
            print(t(f"Couldn't parse the line: {exc}",
                    f"Не удалось разобрать строку: {exc}"), file=sys.stderr)
            continue
        try:
            dispatch(parsed, config, repo_root)
        except Exception as exc:  # keep the session alive on any per-task error
            print(t(f"Error: {exc}", f"Ошибка: {exc}"), file=sys.stderr)


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
    try:
        config = load_config()
    except (KeyError, ValueError, OSError) as exc:
        detail = (t(f"missing key {exc}", f"отсутствует ключ {exc}")
                  if isinstance(exc, KeyError) else str(exc))
        print(t(f"config.toml error: {detail}\n"
                "Check ~/.orchestrator/config.toml (or the built-in config).",
                f"Ошибка в config.toml: {detail}\n"
                "Проверь ~/.orchestrator/config.toml (или встроенный конфиг)."),
              file=sys.stderr)
        return 7
    set_lang(os.environ.get("RELAY_LANG") or config.lang)
    repo_root = Path.cwd()

    # Bare `relay` (no arguments at all) opens the interactive REPL.
    if not argv:
        return interactive(config, repo_root)

    return _dispatch(args, config, repo_root)
