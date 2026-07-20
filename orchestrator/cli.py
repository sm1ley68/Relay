from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import budget, escalate, journal, router, runner
from .config import Config, LADDER, load_config, load_env_file

PREFIXES = {f"/l{i}": f"L{i}" for i in range(5)}

ACCENT = (215, 138, 126)  # Claude-ish salmon
DIM = (140, 140, 140)


def _color(text: str, rgb=ACCENT, *, bold=False) -> str:
    if not sys.stdout.isatty():
        return text
    r, g, b = rgb
    prefix = ("\x1b[1;" if bold else "\x1b[") + f"38;2;{r};{g};{b}m"
    return f"{prefix}{text}\x1b[0m"


def _banner(repo_root: Path, config: Config) -> str:
    cw = 60  # inner content width
    user = (Path.home().name or "there").capitalize()
    cwd = str(repo_root)
    home = str(Path.home())
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]

    def _short(model: str) -> str:
        return model.split("/")[0].split(":")[0]  # provider / bare name

    def _rung(lvl: str) -> str:
        return f"{lvl} {_short(config.levels[lvl].models[0])}"

    body = [
        "",
        f"  Привет, {user}!",
        "",
        "  Опиши задачу обычным языком — уровень и модель",
        "  оркестратор выберет сам. Дешёвые модели снизу,",
        "  Claude (Pro) — только для сложного сверху.",
        "",
        "  " + " · ".join(_rung(lvl) for lvl in LADDER[:3]),
        "  " + " · ".join(_rung(lvl) for lvl in LADDER[3:]),
        "",
        "  " + cwd,
        "",
    ]

    title = "Relay v0.1.0"
    fill = cw + 2 - (len(title) + 3)
    top = _color("╭─ " + title + " " + "─" * fill + "╮")
    bottom = _color("╰" + "─" * (cw + 2) + "╯")
    bar = _color("│")
    lines = [top]
    for row in body:
        row = row[:cw]
        lines.append(f"{bar} {row:<{cw}} {bar}")
    lines.append(bottom)
    hint = _color(
        "  задача — авто-выбор · /l0../l4 — форс · --dry-run — показать · "
        "journal · exit", DIM)
    return "\n".join(lines) + "\n" + hint


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

        if decision.level == "L4" and not args.dry_run and not pro.can_run():
            print("Окно Claude Pro на исходе — поставьте задачу в очередь "
                  "или подождите сброса лимита.", file=sys.stderr)
            entry = journal.new_entry(args.task, decision.level, decision.basis,
                                      decision.framework, "", "pro-exhausted",
                                      0, 0.0, escalations)
            journal_append(entry, Path(config.journal_path).expanduser())
            return 2

        if decision.level == "L4" and not args.dry_run:
            pro.record_run()

        result = run(decision, prompt, config, max_steps, args.dry_run)

        if args.dry_run:
            return 0

        reason = detect(result, config, repo_root)

        if reason is None:
            entry = journal.new_entry(args.task, decision.level, decision.basis,
                                      decision.framework, result.model,
                                      "success", 0, 0.0, escalations)
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
        print("Не git-репозиторий. Запуск агента запрещён предохранителем. "
              "Выполните `git init` или добавьте --no-commit-guard.",
              file=sys.stderr)
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
