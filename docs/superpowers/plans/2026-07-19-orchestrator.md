# AI-Model Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single CLI command that takes a natural-language task, classifies its difficulty, and routes it to the cheapest capable agentic framework/model — sparing the Claude Pro limit — with manual level prefixes, heuristics, an LLM classifier, and automatic context-carrying escalation.

**Architecture:** A thin Python routing layer over two existing agentic frameworks. `opencode` runs cheap OpenRouter models (levels L0–L3); `claude` runs Pro (L4). A router picks a start level (explicit prefix → heuristics → cheap-LLM score), a runner spawns the framework via subprocess, an escalate loop re-runs one level up on failure carrying a journal + `git diff`, budget tracks OpenRouter cost and a rolling Pro-window counter, and a JSONL journal records every decision. No agent loop of our own — file editing, bash, and rollback already live in the frameworks.

**Tech Stack:** Python 3.13, stdlib only at runtime (`subprocess`, `urllib.request`, `tomllib`, `json`, `dataclasses`, `pathlib`). `pytest` for tests.

## Global Constraints

- Python 3.13; **no runtime dependencies outside the standard library**. `pytest` is a dev-only dependency.
- Model ladder levels are `L0`,`L1`,`L2`,`L3`,`L4`. Each level holds a **list** of fallback model IDs, never a single hard ID (OpenRouter free tier rotates).
- L0–L3 → framework `opencode`; L4 → framework `claude`. Framework invocation is a **config template string**, editable in `config.toml` (binaries may not be installed yet).
- Journal format is **JSONL** — one decision per line.
- Never spawn an agent unless the working directory is a git repo and a checkpoint commit was made first (unless `--no-commit-guard`).
- All user-facing errors for missing binaries / missing `OPENROUTER_API_KEY` must be a clear message, never a raw traceback.
- Every code step is TDD: write failing test → run it red → minimal impl → run green → commit.

---

### Task 1: Project scaffold + config module

**Files:**
- Create: `pyproject.toml`
- Create: `orchestrator/__init__.py`
- Create: `orchestrator/config.py`
- Create: `orchestrator/config.toml`
- Create: `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `@dataclass Level: name: str; models: list[str]; price_in: float; price_out: float; framework: str`
  - `@dataclass Config: levels: dict[str, Level]; opencode_cmd: str; claude_cmd: str; max_steps: int; cost_ceiling_usd: float; pro_window_hours: float; pro_window_max_runs: int; classifier_model: str; journal_path: str; budget_path: str; test_cmd: str | None`
  - `DEFAULT_CONFIG_PATH: Path` (points at packaged `config.toml`)
  - `load_config(path: Path | None = None) -> Config`
  - `LADDER: list[str] = ["L0", "L1", "L2", "L3", "L4"]`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "orchestrator"
version = "0.1.0"
description = "AI-model orchestrator: routes NL tasks to the cheapest capable agentic framework"
requires-python = ">=3.13"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
orchestrator = "orchestrator.cli:main"

[tool.setuptools]
packages = ["orchestrator"]

[tool.setuptools.package-data]
orchestrator = ["config.toml"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create empty `orchestrator/__init__.py` and `tests/__init__.py`**

Both files are empty.

- [ ] **Step 3: Create `orchestrator/config.toml` (default ladder + settings)**

```toml
max_steps = 40
cost_ceiling_usd = 0.50
pro_window_hours = 5.0
pro_window_max_runs = 40
classifier_model = "cohere/north-mini-code:free"
journal_path = "~/.orchestrator/journal.jsonl"
budget_path = "~/.orchestrator/budget.json"
test_cmd = ""

# {model}, {prompt}, {steps} are substituted by runner.py
opencode_cmd = 'opencode run -m {model} "{prompt}"'
claude_cmd = 'claude -p "{prompt}"'

[levels.L0]
models = ["cohere/north-mini-code:free"]
price_in = 0.0
price_out = 0.0
framework = "opencode"

[levels.L1]
models = ["poolside/laguna-m.1:free"]
price_in = 0.0
price_out = 0.0
framework = "opencode"

[levels.L2]
models = ["minimax/minimax-m3"]
price_in = 0.60
price_out = 2.40
framework = "opencode"

[levels.L3]
models = ["moonshotai/kimi-k2.7-code"]
price_in = 0.95
price_out = 4.00
framework = "opencode"

[levels.L4]
models = ["claude"]
price_in = 0.0
price_out = 0.0
framework = "claude"
```

- [ ] **Step 4: Write the failing test**

```python
# tests/test_config.py
from pathlib import Path
from orchestrator.config import load_config, Config, Level, LADDER


def test_load_default_config():
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert LADDER == ["L0", "L1", "L2", "L3", "L4"]
    assert set(cfg.levels) == set(LADDER)
    assert cfg.levels["L0"].framework == "opencode"
    assert cfg.levels["L4"].framework == "claude"
    assert cfg.levels["L2"].models == ["minimax/minimax-m3"]
    assert cfg.levels["L2"].price_out == 2.40
    assert cfg.test_cmd is None  # empty string normalizes to None
    assert "{model}" in cfg.opencode_cmd


def test_toml_override(tmp_path: Path):
    override = tmp_path / "c.toml"
    override.write_text(
        'max_steps = 7\n'
        'cost_ceiling_usd = 0.1\n'
        'pro_window_hours = 5.0\n'
        'pro_window_max_runs = 10\n'
        'classifier_model = "x/y:free"\n'
        'journal_path = "j.jsonl"\n'
        'budget_path = "b.json"\n'
        'test_cmd = "pytest"\n'
        'opencode_cmd = "oc {model} {prompt}"\n'
        'claude_cmd = "claude -p {prompt}"\n'
        '[levels.L0]\n'
        'models = ["a", "b"]\n'
        'price_in = 0.0\n'
        'price_out = 0.0\n'
        'framework = "opencode"\n'
    )
    cfg = load_config(override)
    assert cfg.max_steps == 7
    assert cfg.test_cmd == "pytest"
    assert cfg.levels["L0"].models == ["a", "b"]
```

- [ ] **Step 5: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.config'`

- [ ] **Step 6: Write `orchestrator/config.py`**

```python
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

LADDER: list[str] = ["L0", "L1", "L2", "L3", "L4"]
DEFAULT_CONFIG_PATH: Path = Path(__file__).with_name("config.toml")


@dataclass
class Level:
    name: str
    models: list[str]
    price_in: float
    price_out: float
    framework: str


@dataclass
class Config:
    levels: dict[str, Level]
    opencode_cmd: str
    claude_cmd: str
    max_steps: int
    cost_ceiling_usd: float
    pro_window_hours: float
    pro_window_max_runs: int
    classifier_model: str
    journal_path: str
    budget_path: str
    test_cmd: str | None


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    levels = {
        name: Level(
            name=name,
            models=list(body["models"]),
            price_in=float(body["price_in"]),
            price_out=float(body["price_out"]),
            framework=body["framework"],
        )
        for name, body in raw["levels"].items()
    }

    test_cmd = raw.get("test_cmd") or None

    return Config(
        levels=levels,
        opencode_cmd=raw["opencode_cmd"],
        claude_cmd=raw["claude_cmd"],
        max_steps=int(raw["max_steps"]),
        cost_ceiling_usd=float(raw["cost_ceiling_usd"]),
        pro_window_hours=float(raw["pro_window_hours"]),
        pro_window_max_runs=int(raw["pro_window_max_runs"]),
        classifier_model=raw["classifier_model"],
        journal_path=raw["journal_path"],
        budget_path=raw["budget_path"],
        test_cmd=test_cmd,
    )
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml orchestrator/__init__.py orchestrator/config.py orchestrator/config.toml tests/__init__.py tests/test_config.py
git commit -m "feat: project scaffold and config loading"
```

---

### Task 2: Journal (JSONL decision log)

**Files:**
- Create: `orchestrator/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `@dataclass JournalEntry: timestamp: str; task: str; level: str; basis: str; framework: str; model: str; outcome: str; steps: int; cost_usd: float; escalations: list[str]`
  - `append(entry: JournalEntry, path: Path) -> None` (creates parent dirs, appends one JSON line)
  - `read_all(path: Path) -> list[JournalEntry]` (returns `[]` if file missing)
  - `new_entry(task: str, level: str, basis: str, framework: str, model: str, outcome: str, steps: int, cost_usd: float, escalations: list[str]) -> JournalEntry` (fills `timestamp` with UTC ISO-8601)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_journal.py
from pathlib import Path
from orchestrator.journal import JournalEntry, append, read_all, new_entry


def test_append_and_read_roundtrip(tmp_path: Path):
    p = tmp_path / "sub" / "journal.jsonl"  # parent dir does not exist yet
    e1 = new_entry("fix auth", "L4", "prefix", "claude", "claude",
                   "success", 12, 0.0, [])
    e2 = new_entry("rename var", "L0", "heuristic:down-keyword", "opencode",
                   "cohere/north-mini-code:free", "success", 3, 0.0, ["L0->L1"])
    append(e1, p)
    append(e2, p)

    rows = read_all(p)
    assert len(rows) == 2
    assert rows[0].task == "fix auth"
    assert rows[0].level == "L4"
    assert rows[1].escalations == ["L0->L1"]
    assert isinstance(rows[0], JournalEntry)


def test_read_missing_file_returns_empty(tmp_path: Path):
    assert read_all(tmp_path / "nope.jsonl") == []


def test_new_entry_sets_timestamp():
    e = new_entry("t", "L1", "b", "opencode", "m", "success", 1, 0.0, [])
    assert e.timestamp.endswith("Z") or "+00:00" in e.timestamp
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_journal.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.journal'`

- [ ] **Step 3: Write `orchestrator/journal.py`**

```python
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class JournalEntry:
    timestamp: str
    task: str
    level: str
    basis: str
    framework: str
    model: str
    outcome: str
    steps: int
    cost_usd: float
    escalations: list[str] = field(default_factory=list)


def new_entry(task: str, level: str, basis: str, framework: str, model: str,
              outcome: str, steps: int, cost_usd: float,
              escalations: list[str]) -> JournalEntry:
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return JournalEntry(ts, task, level, basis, framework, model, outcome,
                        steps, cost_usd, list(escalations))


def append(entry: JournalEntry, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


def read_all(path: Path) -> list[JournalEntry]:
    path = Path(path)
    if not path.exists():
        return []
    rows: list[JournalEntry] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(JournalEntry(**json.loads(line)))
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_journal.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/journal.py tests/test_journal.py
git commit -m "feat: JSONL decision journal"
```

---

### Task 3: Budget (OpenRouter cost + Pro-window counter)

**Files:**
- Create: `orchestrator/budget.py`
- Test: `tests/test_budget.py`

**Interfaces:**
- Consumes: `orchestrator.config.Level`.
- Produces:
  - `cost_for(level: Level, tokens_in: int, tokens_out: int) -> float` (price is per-1M tokens)
  - `class ProWindow`:
    - `__init__(self, path: Path, window_hours: float, max_runs: int)`
    - `record_run(self, now: float | None = None) -> None` (persists a unix timestamp)
    - `runs_in_window(self, now: float | None = None) -> int`
    - `can_run(self, now: float | None = None) -> bool` (`runs_in_window < max_runs`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_budget.py
from pathlib import Path
from orchestrator.config import Level
from orchestrator.budget import cost_for, ProWindow


def test_cost_for_per_million():
    lvl = Level("L2", ["minimax/minimax-m3"], 0.60, 2.40, "opencode")
    # 1M in, 1M out
    assert cost_for(lvl, 1_000_000, 1_000_000) == 3.0
    # free level
    free = Level("L0", ["x:free"], 0.0, 0.0, "opencode")
    assert cost_for(free, 500_000, 500_000) == 0.0


def test_pro_window_counts_and_expires(tmp_path: Path):
    p = tmp_path / "budget.json"
    w = ProWindow(p, window_hours=5.0, max_runs=2)
    now = 100_000.0
    assert w.runs_in_window(now) == 0
    assert w.can_run(now) is True

    w.record_run(now)
    w.record_run(now + 10)
    assert w.runs_in_window(now + 20) == 2
    assert w.can_run(now + 20) is False

    # 5h + 1s later, both fall out of the window
    later = now + 5 * 3600 + 1
    assert w.runs_in_window(later) == 0
    assert w.can_run(later) is True


def test_pro_window_persists_across_instances(tmp_path: Path):
    p = tmp_path / "budget.json"
    ProWindow(p, 5.0, 5).record_run(1000.0)
    assert ProWindow(p, 5.0, 5).runs_in_window(1001.0) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_budget.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.budget'`

- [ ] **Step 3: Write `orchestrator/budget.py`**

```python
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Level


def cost_for(level: Level, tokens_in: int, tokens_out: int) -> float:
    return (tokens_in * level.price_in + tokens_out * level.price_out) / 1_000_000


class ProWindow:
    def __init__(self, path: Path, window_hours: float, max_runs: int):
        self.path = Path(path)
        self.window_seconds = window_hours * 3600
        self.max_runs = max_runs

    def _load(self) -> list[float]:
        if not self.path.exists():
            return []
        try:
            return [float(x) for x in json.loads(self.path.read_text())]
        except (json.JSONDecodeError, ValueError):
            return []

    def _save(self, stamps: list[float]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(stamps))

    def record_run(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        stamps = self._load()
        stamps.append(now)
        self._save(stamps)

    def runs_in_window(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        return sum(1 for s in self._load() if s >= cutoff)

    def can_run(self, now: float | None = None) -> bool:
        return self.runs_in_window(now) < self.max_runs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_budget.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/budget.py tests/test_budget.py
git commit -m "feat: budget cost accounting and Pro-window counter"
```

---

### Task 4: Router (three-level classification)

**Files:**
- Create: `orchestrator/router.py`
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: `orchestrator.config.Config`, `orchestrator.config.LADDER`.
- Produces:
  - `@dataclass RouteDecision: level: str; framework: str; models: list[str]; basis: str`
  - `UP_KEYWORDS: list[str]`, `DOWN_KEYWORDS: list[str]` (Russian + English)
  - `score_to_level(score: int) -> str` (clamps 1..5 → L0..L4)
  - `estimate_file_count(task: str, repo_root: Path) -> int` (extract quoted/word-like tokens, grep repo, count distinct files)
  - `heuristic_level(task: str, repo_root: Path, already_failed: bool) -> tuple[str | None, str]` (returns `(level, reason)` or `(None, "")`)
  - `llm_score(task: str, model: str, api_key: str, *, _opener=None) -> int` (POST to OpenRouter chat completions; `_opener` injectable for tests)
  - `classify(task: str, config: Config, *, explicit_level: str | None = None, repo_root: Path, already_failed: bool = False, score_fn=None) -> RouteDecision` — `score_fn(task) -> int` overrides the LLM call in tests

- [ ] **Step 1: Write the failing test**

```python
# tests/test_router.py
from pathlib import Path
from orchestrator.config import load_config, LADDER
from orchestrator.router import (
    RouteDecision, score_to_level, heuristic_level, classify, estimate_file_count,
)

CFG = load_config()


def test_score_to_level_clamps():
    assert score_to_level(1) == "L0"
    assert score_to_level(5) == "L4"
    assert score_to_level(0) == "L0"
    assert score_to_level(9) == "L4"


def test_explicit_level_wins(tmp_path: Path):
    d = classify("anything at all", CFG, explicit_level="L4",
                 repo_root=tmp_path, score_fn=lambda t: 1)
    assert d.level == "L4"
    assert d.framework == "claude"
    assert d.basis == "explicit"


def test_heuristic_down_keyword(tmp_path: Path):
    level, reason = heuristic_level("добавь докстринг к функции foo",
                                    tmp_path, already_failed=False)
    assert level in ("L0", "L1")
    assert "down" in reason


def test_heuristic_up_keyword(tmp_path: Path):
    level, reason = heuristic_level("перепиши архитектуру модуля billing",
                                    tmp_path, already_failed=False)
    assert level == "L4"
    assert "up" in reason


def test_already_failed_forces_none_to_escalate(tmp_path: Path):
    # a task the heuristics would send down, but it already failed low
    level, reason = heuristic_level("переименуй переменную x", tmp_path,
                                    already_failed=True)
    assert level == "L4"
    assert "failed" in reason


def test_classify_falls_through_to_llm(tmp_path: Path):
    # neutral task, no keyword hits -> LLM score used
    d = classify("сделай что-нибудь непонятное с кодом", CFG,
                 repo_root=tmp_path, score_fn=lambda t: 3)
    assert d.level == "L2"
    assert d.basis.startswith("llm")


def test_estimate_file_count(tmp_path: Path):
    (tmp_path / "billing.py").write_text("x = 1\n")
    (tmp_path / "other.py").write_text("y = 2\n")
    n = estimate_file_count("правь billing", tmp_path)
    assert n >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.router'`

- [ ] **Step 3: Write `orchestrator/router.py`**

```python
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


@dataclass
class RouteDecision:
    level: str
    framework: str
    models: list[str]
    basis: str


def score_to_level(score: int) -> str:
    idx = max(1, min(5, score)) - 1
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
    return len(hit_files)


def heuristic_level(task: str, repo_root: Path,
                    already_failed: bool) -> tuple[str | None, str]:
    if already_failed:
        return "L4", "failed-low"

    low = task.lower()
    if any(k in low for k in UP_KEYWORDS):
        return "L4", "up-keyword"
    if any(k in low for k in DOWN_KEYWORDS):
        return "L0", "down-keyword"

    files = estimate_file_count(task, repo_root)
    if files >= 4:
        return "L4", f"up-files:{files}"
    if files >= 2:
        return "L3", f"files:{files}"
    return None, ""


def llm_score(task: str, model: str, api_key: str, *, _opener=None) -> int:
    prompt = (
        "Оцени сложность задачи для агента от 1 до 5. "
        "1 = тривиально, 5 = смена архитектуры. Ответь ОДНОЙ цифрой.\n\n"
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
    content = data["choices"][0]["message"]["content"]
    m = re.search(r"[1-5]", content)
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
    else:
        import os
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        score = llm_score(task, config.classifier_model, api_key)
    return _decision(score_to_level(score), config, f"llm:{score}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_router.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/router.py tests/test_router.py
git commit -m "feat: three-level task router"
```

---

### Task 5: Runner (spawn frameworks with fallbacks)

**Files:**
- Create: `orchestrator/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `orchestrator.config.Config`, `orchestrator.router.RouteDecision`.
- Produces:
  - `@dataclass RunResult: exit_code: int; stdout: str; stderr: str; model: str; step_limit_hit: bool`
  - `class FrameworkNotFound(Exception)` (message names the missing binary)
  - `build_command(template: str, model: str, prompt: str, steps: int) -> list[str]` (uses `shlex.split` after substitution)
  - `run_framework(decision: RouteDecision, prompt: str, config: Config, max_steps: int, *, dry_run: bool = False, _runner=None) -> RunResult` — tries each model in `decision.models` until one runs; `_runner(argv) -> (exit_code, stdout, stderr)` injectable for tests

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runner.py
import pytest
from orchestrator.config import load_config
from orchestrator.router import RouteDecision
from orchestrator.runner import (
    run_framework, build_command, RunResult, FrameworkNotFound,
)

CFG = load_config()


def test_build_command_substitutes():
    argv = build_command('opencode run -m {model} "{prompt}"',
                         "minimax/minimax-m3", "fix bug", 40)
    assert argv[:3] == ["opencode", "run", "-m"]
    assert "minimax/minimax-m3" in argv
    assert "fix bug" in argv


def test_dry_run_does_not_spawn():
    dec = RouteDecision("L2", "opencode", ["minimax/minimax-m3"], "explicit")
    called = []
    res = run_framework(dec, "task", CFG, 40, dry_run=True,
                        _runner=lambda argv: called.append(argv))
    assert res.exit_code == 0
    assert called == []  # nothing spawned


def test_runner_returns_result():
    dec = RouteDecision("L2", "opencode", ["m1"], "explicit")
    res = run_framework(dec, "task", CFG, 40,
                        _runner=lambda argv: (0, "done", ""))
    assert isinstance(res, RunResult)
    assert res.exit_code == 0
    assert res.model == "m1"


def test_runner_tries_fallback_on_missing_binary():
    dec = RouteDecision("L2", "opencode", ["m1", "m2"], "explicit")
    calls = []

    def runner(argv):
        calls.append(argv)
        if "m1" in argv:
            raise FileNotFoundError("opencode")
        return (0, "ok", "")

    res = run_framework(dec, "task", CFG, 40, _runner=runner)
    assert res.model == "m2"
    assert len(calls) == 2


def test_runner_raises_when_all_missing():
    dec = RouteDecision("L2", "opencode", ["m1"], "explicit")

    def runner(argv):
        raise FileNotFoundError("opencode")

    with pytest.raises(FrameworkNotFound):
        run_framework(dec, "task", CFG, 40, _runner=runner)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.runner'`

- [ ] **Step 3: Write `orchestrator/runner.py`**

```python
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
    filled = (template
              .replace("{model}", model)
              .replace("{prompt}", prompt)
              .replace("{steps}", str(steps)))
    return shlex.split(filled)


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_runner.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/runner.py tests/test_runner.py
git commit -m "feat: framework runner with model fallbacks"
```

---

### Task 6: Escalate (failure detection + context assembly)

**Files:**
- Create: `orchestrator/escalate.py`
- Test: `tests/test_escalate.py`

**Interfaces:**
- Consumes: `orchestrator.runner.RunResult`, `orchestrator.config.Config`, `orchestrator.config.LADDER`.
- Produces:
  - `next_level(level: str) -> str | None` (returns next up the ladder, or `None` at L4)
  - `detect_loop(stdout: str, threshold: int = 3) -> bool` (any identical non-blank line repeated `>= threshold` times)
  - `detect_failure(result: RunResult, config: Config, repo_root: Path, *, _test_runner=None) -> str | None` (returns reason or `None`; checks exit code, `step_limit_hit`, loop, and `config.test_cmd`)
  - `git_diff(repo_root: Path, *, _runner=None) -> str`
  - `build_escalation_prompt(original_task: str, attempts: list[str], repo_root: Path, *, _runner=None) -> str` (task journal text + `git diff`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_escalate.py
from pathlib import Path
from orchestrator.config import load_config
from orchestrator.runner import RunResult
from orchestrator.escalate import (
    next_level, detect_loop, detect_failure, build_escalation_prompt,
)

CFG = load_config()


def test_next_level():
    assert next_level("L0") == "L1"
    assert next_level("L3") == "L4"
    assert next_level("L4") is None


def test_detect_loop():
    text = "call foo\ncall foo\ncall foo\n"
    assert detect_loop(text, threshold=3) is True
    assert detect_loop("a\nb\nc\n", threshold=3) is False


def test_detect_failure_on_exit_code():
    res = RunResult(1, "", "boom", "m", False)
    assert detect_failure(res, CFG, Path(".")) == "exit-code:1"


def test_detect_failure_on_step_limit():
    res = RunResult(0, "", "", "m", True)
    assert detect_failure(res, CFG, Path(".")) == "step-limit"


def test_detect_failure_none_on_success():
    res = RunResult(0, "all good", "", "m", False)
    assert detect_failure(res, CFG, Path(".")) is None


def test_detect_failure_test_cmd(tmp_path: Path):
    import dataclasses
    cfg = dataclasses.replace(CFG, test_cmd="pytest")
    res = RunResult(0, "", "", "m", False)
    # test command reports failure via injected runner
    reason = detect_failure(res, cfg, tmp_path,
                            _test_runner=lambda cmd, cwd: 1)
    assert reason == "tests-failed"


def test_build_escalation_prompt_includes_diff_and_attempts(tmp_path: Path):
    prompt = build_escalation_prompt(
        "fix auth", ["L1: exit-code:1"], tmp_path,
        _runner=lambda root: "diff --git a/x b/x\n+changed",
    )
    assert "fix auth" in prompt
    assert "L1: exit-code:1" in prompt
    assert "diff --git" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_escalate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.escalate'`

- [ ] **Step 3: Write `orchestrator/escalate.py`**

```python
from __future__ import annotations

import shlex
import subprocess
from collections import Counter
from pathlib import Path

from .config import LADDER, Config
from .runner import RunResult


def next_level(level: str) -> str | None:
    idx = LADDER.index(level)
    return LADDER[idx + 1] if idx + 1 < len(LADDER) else None


def detect_loop(stdout: str, threshold: int = 3) -> bool:
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return False
    counts = Counter(lines)
    return max(counts.values()) >= threshold


def _run_test_cmd(cmd: str, cwd: Path) -> int:
    proc = subprocess.run(shlex.split(cmd), cwd=str(cwd),
                          capture_output=True, text=True)
    return proc.returncode


def detect_failure(result: RunResult, config: Config, repo_root: Path, *,
                   _test_runner=None) -> str | None:
    if result.exit_code != 0:
        return f"exit-code:{result.exit_code}"
    if result.step_limit_hit:
        return "step-limit"
    if detect_loop(result.stdout):
        return "loop"
    if config.test_cmd:
        runner = _test_runner or _run_test_cmd
        if runner(config.test_cmd, repo_root) != 0:
            return "tests-failed"
    return None


def git_diff(repo_root: Path, *, _runner=None) -> str:
    if _runner is not None:
        return _runner(repo_root)
    proc = subprocess.run(["git", "diff"], cwd=str(repo_root),
                          capture_output=True, text=True)
    return proc.stdout


def build_escalation_prompt(original_task: str, attempts: list[str],
                            repo_root: Path, *, _runner=None) -> str:
    diff = git_diff(repo_root, _runner=_runner)
    attempt_lines = "\n".join(f"- {a}" for a in attempts)
    return (
        f"Исходная задача:\n{original_task}\n\n"
        f"Предыдущие попытки провалились:\n{attempt_lines}\n\n"
        f"Текущее состояние (git diff):\n{diff}\n\n"
        "Файлы уже на диске. Продолжи с этого состояния и доведи задачу до конца."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_escalate.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/escalate.py tests/test_escalate.py
git commit -m "feat: escalation failure detection and context assembly"
```

---

### Task 7: CLI (parsing, git guard, orchestration loop)

**Files:**
- Create: `orchestrator/cli.py`
- Create: `orchestrator/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: all previous modules.
- Produces:
  - `@dataclass ParsedArgs: command: str; task: str; explicit_level: str | None; max_steps: int | None; test_cmd: str | None; dry_run: bool; no_commit_guard: bool`
  - `parse_args(argv: list[str]) -> ParsedArgs` (extracts `/l0`../`l4` prefix, flags, subcommands `run`/`rollback`/`journal`)
  - `ensure_git_repo(root: Path) -> bool`
  - `checkpoint_commit(root: Path, *, _runner=None) -> str` (returns sha; commits all current state)
  - `rollback(root: Path, sha: str, *, _runner=None) -> None`
  - `orchestrate(args: ParsedArgs, config: Config, repo_root: Path, *, deps=None) -> int` (full router→runner→escalate→budget→journal loop; `deps` injects classify/run/detect for tests)
  - `main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
from pathlib import Path
from orchestrator.config import load_config
from orchestrator.cli import parse_args, orchestrate, ParsedArgs
from orchestrator.runner import RunResult

CFG = load_config()


def test_parse_prefix_and_flags():
    a = parse_args(["/l4", "fix", "the", "auth", "bug", "--max-steps", "10"])
    assert a.command == "run"
    assert a.explicit_level == "L4"
    assert a.task == "fix the auth bug"
    assert a.max_steps == 10


def test_parse_no_prefix():
    a = parse_args(["rename", "variable", "x"])
    assert a.explicit_level is None
    assert a.task == "rename variable x"


def test_parse_dry_run_and_subcommand():
    a = parse_args(["--dry-run", "do", "thing"])
    assert a.dry_run is True
    r = parse_args(["rollback"])
    assert r.command == "rollback"


def test_orchestrate_success_path(tmp_path: Path):
    # inject deps so no real subprocess/network happens
    calls = {"journal": []}
    deps = {
        "classify": lambda task, cfg, **kw: __import__(
            "orchestrator.router", fromlist=["RouteDecision"]
        ).RouteDecision("L0", "opencode", ["m"], "explicit"),
        "run": lambda dec, prompt, cfg, steps, dry_run: RunResult(0, "ok", "", "m", False),
        "detect_failure": lambda res, cfg, root: None,
        "checkpoint": lambda root: "abc123",
        "journal_append": lambda entry, path: calls["journal"].append(entry),
    }
    args = ParsedArgs("run", "do a thing", "L0", None, None, False, True)
    code = orchestrate(args, CFG, tmp_path, deps=deps)
    assert code == 0
    assert len(calls["journal"]) == 1
    assert calls["journal"][0].outcome == "success"
    assert calls["journal"][0].level == "L0"


def test_orchestrate_escalates_on_failure(tmp_path: Path):
    from orchestrator.router import RouteDecision
    attempts = {"n": 0}
    journal = []

    def run(dec, prompt, cfg, steps, dry_run):
        attempts["n"] += 1
        # L0 fails, L1 succeeds
        return RunResult(0 if dec.level == "L1" else 1, "", "", "m", False)

    deps = {
        "classify": lambda task, cfg, **kw: RouteDecision(
            kw.get("explicit_level") or "L0", "opencode", ["m"], "explicit"),
        "run": run,
        "detect_failure": lambda res, cfg, root: (
            None if res.exit_code == 0 else "exit-code:1"),
        "checkpoint": lambda root: "abc",
        "journal_append": lambda entry, path: journal.append(entry),
    }
    args = ParsedArgs("run", "hard task", "L0", None, None, False, True)
    code = orchestrate(args, CFG, tmp_path, deps=deps)
    assert code == 0
    assert attempts["n"] == 2          # escalated L0 -> L1
    assert journal[-1].level == "L1"
    assert journal[-1].escalations == ["L0->L1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.cli'`

- [ ] **Step 3: Write `orchestrator/cli.py`**

```python
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import budget, escalate, journal, router, runner
from .config import Config, LADDER, load_config

PREFIXES = {f"/l{i}": f"L{i}" for i in range(5)}


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
    run(["git", "add", "-A"])
    run(["git", "commit", "-m", "orchestrator: checkpoint", "--allow-empty"])
    proc = run(["git", "rev-parse", "HEAD"])
    return proc.stdout.strip()


def rollback(root: Path, sha: str, *, _runner=None) -> None:
    argv = ["git", "reset", "--hard", sha]
    if _runner is not None:
        _runner(argv)
    else:
        subprocess.run(argv, cwd=str(root))


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

    if not args.no_commit_guard:
        checkpoint(repo_root)

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

        if decision.level == "L4" and not pro.can_run():
            print("Окно Claude Pro на исходе — поставьте задачу в очередь "
                  "или подождите сброса лимита.", file=sys.stderr)
            entry = journal.new_entry(args.task, decision.level, decision.basis,
                                      decision.framework, "", "pro-exhausted",
                                      0, 0.0, escalations)
            journal_append(entry, Path(config.journal_path).expanduser())
            return 2

        if decision.level == "L4":
            pro.record_run()

        result = run(decision, prompt, config, max_steps, args.dry_run)
        reason = None if args.dry_run else detect(result, config, repo_root)

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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    config = load_config()
    repo_root = Path.cwd()

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
        print("Пустая задача. Пример: orchestrator /l2 почини авторизацию",
              file=sys.stderr)
        return 3

    try:
        return orchestrate(args, config, repo_root)
    except runner.FrameworkNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 4
```

- [ ] **Step 4: Write `orchestrator/__main__.py`**

```python
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -v`
Expected: PASS (all tasks' tests green)

- [ ] **Step 7: Smoke-test dry-run end to end**

Run:
```bash
python -m orchestrator /l0 --dry-run --no-commit-guard "add a docstring to foo"
```
Expected: prints a `[dry-run] opencode run -m cohere/north-mini-code:free "..."` line and exits 0.

- [ ] **Step 8: Commit**

```bash
git add orchestrator/cli.py orchestrator/__main__.py tests/test_cli.py
git commit -m "feat: CLI parsing, git guard, and orchestration loop"
```

---

### Task 8: README + usage docs

**Files:**
- Modify: `README.md`
- Test: none (docs only) — verified by the smoke command below.

- [ ] **Step 1: Replace `README.md`**

```markdown
# Orchestrator — AI-model router

Single CLI that routes a natural-language task to the cheapest capable agentic
framework: `opencode` + OpenRouter for levels L0–L3 (free/cheap), `claude` (Pro)
for L4. Conserves the Claude Pro limit by sending routine work to cheap models
and escalating only on failure.

## Install

    pip install -e .

Requires: Python 3.13, `opencode` and `claude` on PATH, `OPENROUTER_API_KEY` set.

## Use

    orchestrator "rename the variable foo to bar"        # auto-routed
    orchestrator /l4 "redesign the billing module"       # forced level
    orchestrator --dry-run "add a test for parse_args"    # show, don't run
    orchestrator journal                                  # view decision log

Levels: `/l0` trivial · `/l1` simple edits · `/l2` workhorse · `/l3` long
sessions · `/l4` architecture / hard bugs.

## How routing works

1. Explicit `/lN` prefix wins.
2. Heuristics: keyword lists + grep-based file count.
3. Cheap-LLM score (1–5) for the rest.

On failure (nonzero exit, step limit, loop, failing tests) the task escalates one
level up, carrying a journal + `git diff`. A checkpoint commit is made before any
agent runs — roll back with `git reset --hard <checkpoint>`.

## Config

Edit `orchestrator/config.toml`: model ladder (fallback lists), framework command
templates, step limit, cost ceiling, Pro-window thresholds.
```

- [ ] **Step 2: Verify install + smoke test**

Run:
```bash
pip install -e . && orchestrator --dry-run --no-commit-guard /l0 "add docstring"
```
Expected: `[dry-run] ...` line, exit 0.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: usage README"
```

---

## Self-Review

**Spec coverage:**
- §1 цель → whole system. ✓
- §2 схema (classifier → opencode/claude, escalation) → Task 4/5/6/7. ✓
- §3 routing principle (default down, up on explicit/failure) → `heuristic_level` + escalation loop. ✓
- §4 ladder (L0–L4, fallback lists, prices) → Task 1 config.toml, Task 3 cost. ✓
- §5 classification (3 levels) → Task 4. ✓
- §6 escalation (triggers, context transfer, Pro-window check, reverse-escalation note) → Task 6 + orchestrate loop; reverse-escalation recorded as `pro-exhausted` journal outcome. ✓
- §7 safeguards (checkpoint commit, step limit, cost ceiling, journal) → Task 7 checkpoint + `max_steps` + Task 1 `cost_ceiling_usd` + Task 2 journal. ✓
- §8 module layout (cli/router/runner/escalate/budget/journal) → Tasks 1–7 map 1:1. ✓
- §9 staged order → superseded by user decision (full ladder now), noted in spec §2. ✓
- §10/§11 open questions → journal JSONL, file-count via keywords+grep, Pro-window heuristic, framework CLI in config. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✓

**Type consistency:** `RouteDecision(level, framework, models, basis)` used identically in Tasks 4/5/7; `RunResult(exit_code, stdout, stderr, model, step_limit_hit)` in Tasks 5/6/7; `JournalEntry` fields/`new_entry` signature consistent Tasks 2/7; `config.test_cmd` (str|None) consistent Tasks 1/6/7. ✓

**Note:** `cost_ceiling_usd` and detailed token-cost accumulation are wired as config + `cost_for`, but the orchestrate loop logs `cost_usd=0.0` (real token counts are not exposed by the framework CLIs in text mode). This is an honest limitation, consistent with spec §11's caveat that per-call cost data may not be reliably available. Left as-is rather than fabricating numbers.
