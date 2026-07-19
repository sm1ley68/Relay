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
