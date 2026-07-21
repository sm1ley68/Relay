from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

LADDER: list[str] = ["L0", "L1", "L2", "L3"]
DEFAULT_CONFIG_PATH: Path = Path(__file__).with_name("config.toml")


GLOBAL_ENV_PATH: Path = Path.home() / ".orchestrator" / ".env"


def _apply_env_file(path: Path) -> None:
    """Parse one .env file into os.environ (no override of existing keys)."""
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


def load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env file(s) into os.environ.

    With no argument, reads ``./.env`` (project override) then the global
    ``~/.orchestrator/.env`` — so a globally-installed ``relay`` finds the key
    from any working directory. A given ``path`` loads just that file.

    Stdlib-only (no python-dotenv). Missing files are a no-op. Existing
    environment variables are never overridden (a real shell export wins, and
    the project .env wins over the global one). Blank lines and ``#`` comments
    are ignored; an optional ``export`` prefix and surrounding quotes stripped.
    """
    paths = [path] if path is not None else [Path.cwd() / ".env", GLOBAL_ENV_PATH]
    for p in paths:
        _apply_env_file(p)


GLOBAL_CONFIG_PATH: Path = Path.home() / ".orchestrator" / "config.toml"


@dataclass
class Framework:
    """A pluggable agent CLI: how to invoke it and read its output."""
    name: str
    cmd: str            # command template with {model}/{prompt}/{steps}
    format: str         # "opencode-json" | "claude-json" | "text"
    auto: list[str]     # flags that auto-approve the agent's actions


@dataclass
class Level:
    name: str
    models: list[str]
    price_in: float
    price_out: float
    framework: str
    metered: bool = False  # subscription-gated (counts against the usage window)


@dataclass
class Config:
    levels: dict[str, Level]
    frameworks: dict[str, Framework]
    max_steps: int
    cost_ceiling_usd: float
    pro_window_hours: float
    pro_window_max_runs: int
    classifier_model: str
    journal_path: str
    budget_path: str
    test_cmd: str | None
    task_timeout_seconds: int
    active_path: str = ""


def _resolve_config_path(path: Path | None) -> Path:
    """arg > $RELAY_CONFIG > ~/.orchestrator/config.toml > packaged default."""
    if path is not None:
        return path
    env = os.environ.get("RELAY_CONFIG")
    if env:
        return Path(env).expanduser()
    if GLOBAL_CONFIG_PATH.exists():
        return GLOBAL_CONFIG_PATH
    return DEFAULT_CONFIG_PATH


# built-in output formats per known framework (for back-compat synthesis)
_BUILTIN_FORMATS = {"opencode": "opencode-json", "claude": "claude-json"}


def load_config(path: Path | None = None) -> Config:
    path = _resolve_config_path(path)
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    levels = {
        name: Level(
            name=name,
            models=list(body["models"]),
            price_in=float(body["price_in"]),
            price_out=float(body["price_out"]),
            framework=body["framework"],
            metered=bool(body.get("metered", False)),
        )
        for name, body in raw["levels"].items()
    }

    # Frameworks: prefer an explicit [frameworks.*] section; otherwise synthesize
    # from the legacy opencode_cmd/claude_cmd keys (back-compat).
    frameworks: dict[str, Framework] = {}
    for name, body in raw.get("frameworks", {}).items():
        frameworks[name] = Framework(
            name=name,
            cmd=body["cmd"],
            format=body.get("format", _BUILTIN_FORMATS.get(name, "text")),
            auto=list(body.get("auto", [])),
        )
    for legacy, key in (("opencode", "opencode_cmd"), ("claude", "claude_cmd")):
        if legacy not in frameworks and key in raw:
            frameworks[legacy] = Framework(
                name=legacy, cmd=raw[key],
                format=_BUILTIN_FORMATS[legacy],
                auto=(["--auto"] if legacy == "opencode"
                      else ["--permission-mode", "acceptEdits"]),
            )

    test_cmd = raw.get("test_cmd") or None

    return Config(
        levels=levels,
        frameworks=frameworks,
        max_steps=int(raw["max_steps"]),
        cost_ceiling_usd=float(raw["cost_ceiling_usd"]),
        pro_window_hours=float(raw["pro_window_hours"]),
        pro_window_max_runs=int(raw["pro_window_max_runs"]),
        classifier_model=raw["classifier_model"],
        journal_path=raw["journal_path"],
        budget_path=raw["budget_path"],
        test_cmd=test_cmd,
        task_timeout_seconds=int(raw.get("task_timeout_seconds", 600)),
        active_path=str(path),
    )
