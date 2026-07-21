from __future__ import annotations

import json
import re
import subprocess
import urllib.request
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


@dataclass
class RouteDecision:
    level: str
    framework: str
    models: list[str]
    basis: str


def score_to_level(score: int) -> str:
    # LLM returns 1-5; map onto however many levels the ladder has.
    idx = max(1, min(len(LADDER), score)) - 1
    return LADDER[idx]


def _decision(level: str, config: Config, basis: str) -> RouteDecision:
    lvl = config.levels[level]
    return RouteDecision(level=level, framework=lvl.framework,
                         models=list(lvl.models), basis=basis)


def estimate_file_count(task: str, repo_root: Path) -> int:
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task))
    if not tokens:
        return 0
    hit_files: set[str] = set()
    for tok in tokens:
        try:
            out = subprocess.run(
                ["grep", "-rIl", "--include=*.py", tok, str(repo_root)],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.stdout.splitlines():
            if line.strip():
                hit_files.add(line.strip())
        for path in repo_root.rglob(f"*{tok}*.py"):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(repo_root).parts
            if any(part.startswith(".") or part in
                   {"node_modules", "__pycache__", ".venv", "venv"}
                   for part in rel_parts):
                continue
            hit_files.add(str(path))
    return len(hit_files)


def heuristic_level(task: str, repo_root: Path,
                    already_failed: bool) -> tuple[str | None, str]:
    top = LADDER[-1]  # the Claude (subscription) level, whatever it's numbered
    if already_failed:
        return top, "failed-low"

    low = task.lower()
    if low.strip().strip("!.?…) ") in GREETINGS:
        return "L0", "trivial"
    if any(k in low for k in UP_KEYWORDS):
        return top, "up-keyword"
    if any(k in low for k in DOWN_KEYWORDS):
        return "L0", "down-keyword"

    files = estimate_file_count(task, repo_root)
    if files >= 4:
        return top, f"up-files:{files}"
    if files >= 2:
        return LADDER[-2], f"files:{files}"
    return None, ""


def llm_score(task: str, model: str, api_key: str, *, _opener=None) -> int:
    prompt = (
        "Оцени сложность задачи для программиста от 1 до 5. Шкала:\n"
        "1 = тривиально или это вообще не задача (приветствие, вопрос, "
        "опечатка, докстринг, форматирование).\n"
        "2 = простая правка в одном файле по образцу.\n"
        "3 = обычная задача, пара файлов.\n"
        "4 = много связанных файлов или неочевидный баг.\n"
        "5 = смена архитектуры, переписывание модуля.\n"
        "Если сомневаешься — ставь ниже. Ответь РОВНО ОДНОЙ цифрой.\n\n"
        f"Задача: {task}"
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

    # Level 2: heuristics.
    level, reason = heuristic_level(task, repo_root, already_failed)
    if level is not None:
        return _decision(level, config, f"heuristic:{reason}")

    # Level 3: LLM classifier.
    if score_fn is not None:
        score = score_fn(task)
        return _decision(score_to_level(score), config, f"llm:{score}")

    import os
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        # No OpenRouter key (e.g. a Codex-only setup): skip the network call and
        # default to the workhorse level; heuristics still route the obvious cases.
        return _decision(score_to_level(3), config, "no-classifier")
    try:
        score = llm_score(task, config.classifier_model, api_key)
    except Exception:
        # Network/HTTP/parse failure: degrade to the workhorse level instead
        # of crashing the run.
        return _decision(score_to_level(3), config, "llm-unavailable")
    return _decision(score_to_level(score), config, f"llm:{score}")
