from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


class ProWindow:
    """Rolling count of runs against a subscription's usage window."""

    def __init__(self, path: Path, window_hours: float, max_runs: int):
        self.path = Path(path)
        self.window_seconds = window_hours * 3600
        self.max_runs = max_runs

    def _load(self) -> list[float]:
        if not self.path.exists():
            return []
        try:
            return [float(x) for x in json.loads(self.path.read_text())]
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            return []

    def _save(self, stamps: list[float]) -> None:
        """Write atomically — a crash mid-write must not blank the ledger."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(stamps, fh)
            os.replace(tmp, self.path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

    def record_run(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        # Prune on write: stamps older than the window can never matter again,
        # so the file stays bounded instead of growing for the life of the install.
        cutoff = now - self.window_seconds
        stamps = [s for s in self._load() if s >= cutoff]
        stamps.append(now)
        self._save(stamps)

    def runs_in_window(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        return sum(1 for s in self._load() if s >= cutoff)

    def remaining(self, now: float | None = None) -> int:
        return max(0, self.max_runs - self.runs_in_window(now))

    def resets_in_seconds(self, now: float | None = None) -> float:
        """Seconds until the oldest run in the window ages out (0 if none)."""
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        live = [s for s in self._load() if s >= cutoff]
        return max(0.0, (min(live) + self.window_seconds) - now) if live else 0.0

    def can_run(self, now: float | None = None) -> bool:
        return self.runs_in_window(now) < self.max_runs
