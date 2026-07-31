from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import LADDER, Config

UP_KEYWORDS: list[str] = [
    "архитектур", "миграци", "развяжи", "перепиши", "перепис",
    "architecture", "migrate", "migration", "decouple", "rewrite", "refactor the",
]
DOWN_KEYWORDS: list[str] = [
    "переименуй", "переименован", "добавь тест", "докстринг", "отформатируй",
    "формат", "комментар", "rename", "add test", "docstring", "format", "typo",
]
# Greetings / small talk / trivial pings — not coding tasks; keep them free (L0).
GREETINGS: set[str] = {
    "привет", "приветик", "прив", "здравствуй", "здравствуйте", "хай", "ку",
    "как дела", "спасибо", "спс", "пока", "тест", "проверка", "ping", "pong",
    "hi", "hello", "hey", "yo", "thanks", "thank you", "test", "ok", "ок",
}
# Openers for "explain / what is this" requests. They read code but change
# nothing, so they start at the free level; escalation covers the rest.
QUESTION_OPENERS: tuple[str, ...] = (
    "what ", "why ", "how ", "who ", "when ", "where ", "explain", "describe",
    "summarize", "summarise", "tell me", "show me", "list ", "does ", "is ",
    "can you explain", "что ", "почему", "зачем", "как работает", "объясни",
    "расскажи", "покажи", "опиши", "перечисли",
)

# Words that appear in almost every source file, so grepping for them says
# nothing about how many files a task touches. Without this list an ordinary
# English sentence ("make the help output shorter") matches every .py in the
# repo and routes to the most expensive level — the opposite of the point.
_STOPWORDS: frozenset[str] = frozenset("""
add and any are but can change check code comment could day did does don for
from get has have help here how its just let like make may most need new not
now one only out please put run see set should show some than that the their
them then there these they this those try use used using want was way what when
where which who why will with work would you your file files line lines func
function functions method methods class classes test tests fix update remove
delete write read print return value values name names data type types
""".split())

# Directories whose contents are not the user's code (vendored deps, VCS
# internals) — a match inside them must never inflate the file count.
_EXCLUDED_DIRS: frozenset[str] = frozenset({
    "node_modules", "__pycache__", ".venv", "venv", ".git", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", ".eggs",
})


@dataclass
class RouteDecision:
    level: str
    framework: str
    models: list[str]
    basis: str


def score_to_level(score: int, ladder: list[str] | None = None) -> str:
    # LLM returns 1-5; map onto however many levels the ladder has.
    rungs = ladder or LADDER
    idx = max(1, min(len(rungs), score)) - 1
    return rungs[idx]


def _decision(level: str, config: Config, basis: str) -> RouteDecision:
    lvl = config.levels[level]
    return RouteDecision(level=level, framework=lvl.framework,
                         models=list(lvl.models), basis=basis)


def _is_excluded(path: Path, repo_root: Path) -> bool:
    """True for vendored / VCS / cache paths that aren't the user's own code."""
    try:
        rel_parts = path.relative_to(repo_root).parts
    except ValueError:
        return False
    return any(part.startswith(".") or part in _EXCLUDED_DIRS
               for part in rel_parts[:-1])


def significant_tokens(task: str) -> set[str]:
    """Identifier-ish words worth grepping for — stopwords dropped.

    A token counts only if it could plausibly name something in the code:
    it survives the stopword list and either looks like an identifier
    (``snake_case``, ``CamelCase``, a ``foo.py`` filename) or is a word long
    enough to be domain vocabulary rather than English filler.
    """
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", task):
        tok = raw.rstrip(".")
        stem = tok.split(".")[0]
        if len(stem) < 3 or stem.lower() in _STOPWORDS:
            continue
        identifier_like = ("_" in stem or "." in tok
                           or not stem.islower() and not stem.isupper())
        if identifier_like or len(stem) >= 5:
            tokens.add(stem)
    return tokens


def _project_py_files(repo_root: Path) -> int:
    """How many .py files the project itself has (vendored dirs excluded)."""
    return sum(1 for p in repo_root.rglob("*.py")
               if p.is_file() and not _is_excluded(p, repo_root))


# A token matching a large share of the codebase describes the codebase, not
# the task — it cannot say how many files the task touches, so it is noise.
# This catches the filler words no hand-written stopword list will ever cover.
_NOISE_SHARE = 0.3
_NOISE_FLOOR = 3


def _is_strong(token: str) -> bool:
    """Does this token name code (``snake_case``, ``CamelCase``, ``CONST``)?

    A plain lowercase English word is weak evidence — it shows up in prose and
    comments as readily as in code, so one of them alone must not conclude that
    a task spans the repo.
    """
    return "_" in token or not token.islower()


def estimate_file_count(task: str, repo_root: Path) -> int:
    tokens = significant_tokens(task)
    if not tokens:
        return 0
    excluded_args = [f"--exclude-dir={d}" for d in sorted(_EXCLUDED_DIRS)]
    total_py = _project_py_files(repo_root)
    noise_cutoff = max(_NOISE_FLOOR, int(total_py * _NOISE_SHARE))

    strong_hits: set[str] = set()
    name_hits: set[str] = set()
    weak_counts: Counter[str] = Counter()

    for tok in tokens:
        # A filename match is always strong: naming a file names the target.
        for path in repo_root.rglob(f"*{tok}*.py"):
            if path.is_file() and not _is_excluded(path, repo_root):
                name_hits.add(str(path))
        try:
            out = subprocess.run(
                ["grep", "-rIlw", "--include=*.py", *excluded_args,
                 "--", tok, str(repo_root)],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        hits = {line.strip() for line in out.stdout.splitlines()
                # grep's --exclude-dir misses dot-dirs on some platforms
                if line.strip() and not _is_excluded(Path(line.strip()), repo_root)}
        if len(hits) > noise_cutoff:
            continue                       # matches most of the repo -> tells us nothing
        if _is_strong(tok):
            strong_hits |= hits
        else:
            weak_counts.update(hits)

    # A weak word counts only when a second one corroborates it in the same file.
    weak_hits = {f for f, n in weak_counts.items() if n >= 2}
    return len(strong_hits | name_hits | weak_hits)


def heuristic_level(task: str, repo_root: Path, already_failed: bool,
                    ladder: list[str] | None = None) -> tuple[str | None, str]:
    rungs = ladder or LADDER
    top = rungs[-1]     # the Claude (subscription) level, whatever it's numbered
    bottom = rungs[0]
    if already_failed:
        return top, "failed-low"

    low = task.lower()
    stripped = low.strip().strip("!.?…) ")
    if stripped in GREETINGS:
        return bottom, "trivial"
    if any(k in low for k in UP_KEYWORDS):
        return top, "up-keyword"
    if any(k in low for k in DOWN_KEYWORDS):
        return bottom, "down-keyword"
    # A question reads code but changes nothing — start cheap, escalate if needed.
    if stripped.endswith("?") or stripped.startswith(QUESTION_OPENERS):
        return bottom, "question"

    files = estimate_file_count(task, repo_root)
    if files >= 4:
        return top, f"up-files:{files}"
    if files >= 2 and len(rungs) >= 2:
        return rungs[-2], f"files:{files}"
    return None, ""


def llm_score(task: str, model: str, api_key: str, *, _opener=None) -> int:
    prompt = (
        "Rate the difficulty of this programming task from 1 to 5. Scale:\n"
        "1 = trivial, or not a task at all (greeting, question, typo, "
        "docstring, formatting).\n"
        "2 = a simple single-file edit following an existing pattern.\n"
        "3 = an ordinary task spanning a couple of files.\n"
        "4 = many related files, or a non-obvious bug.\n"
        "5 = an architecture change or rewriting a module.\n"
        "When in doubt, rate LOWER. Answer with EXACTLY ONE digit.\n\n"
        f"Task: {task}"
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4,
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    opener = _opener or urllib.request.urlopen
    with opener(req, timeout=30) as resp:
        data = json.loads(resp.read())
    # Response shape can vary (missing keys, null content); tolerate all of it.
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = ""
    m = re.search(r"[1-5]", content or "")
    return int(m.group()) if m else 3


def classify(task: str, config: Config, *, explicit_level: str | None = None,
             repo_root: Path, already_failed: bool = False,
             score_fn=None) -> RouteDecision:
    # Level 1: explicit prefix wins.
    if explicit_level:
        return _decision(explicit_level, config, "explicit")

    ladder = config.ladder

    # Level 2: heuristics.
    level, reason = heuristic_level(task, repo_root, already_failed, ladder)
    if level is not None:
        return _decision(level, config, f"heuristic:{reason}")

    # Level 3: LLM classifier.
    if score_fn is not None:
        score = score_fn(task)
        return _decision(score_to_level(score, ladder), config, f"llm:{score}")

    import os
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key or not config.classifier_model:
        # No OpenRouter key (e.g. a Codex-only setup): skip the network call and
        # default to the workhorse level; heuristics still route the obvious cases.
        return _decision(score_to_level(3, ladder), config, "no-classifier")
    try:
        score = llm_score(task, config.classifier_model, api_key)
    except Exception:
        # Network/HTTP/parse failure: degrade to the workhorse level instead
        # of crashing the run.
        return _decision(score_to_level(3, ladder), config, "llm-unavailable")
    return _decision(score_to_level(score, ladder), config, f"llm:{score}")
