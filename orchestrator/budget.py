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
