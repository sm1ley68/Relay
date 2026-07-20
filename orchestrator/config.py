from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

LADDER: list[str] = ["L0", "L1", "L2", "L3", "L4"]
DEFAULT_CONFIG_PATH: Path = Path(__file__).with_name("config.toml")


def load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Stdlib-only (no python-dotenv). Missing file is a no-op. Existing
    environment variables are never overridden, so a real shell export
    wins over the file. Blank lines and ``#`` comments are ignored; an
    optional ``export`` prefix and surrounding quotes are stripped.
    """
    path = path or (Path.cwd() / ".env")
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


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
