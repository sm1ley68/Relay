import os
from pathlib import Path
from orchestrator.config import load_config, load_env_file, Config, Level, LADDER


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


def test_load_env_file_sets_var(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        '# a comment\n'
        '\n'
        'export QUOTED_KEY="secret-value"\n'
        "PLAIN_KEY=plain\n"
    )
    monkeypatch.delenv("QUOTED_KEY", raising=False)
    monkeypatch.delenv("PLAIN_KEY", raising=False)
    load_env_file(env)
    assert os.environ["QUOTED_KEY"] == "secret-value"  # quotes + export stripped
    assert os.environ["PLAIN_KEY"] == "plain"


def test_load_env_file_does_not_override_existing(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('SHELL_WINS=from-file\n')
    monkeypatch.setenv("SHELL_WINS", "from-shell")
    load_env_file(env)
    assert os.environ["SHELL_WINS"] == "from-shell"  # real env beats the file


def test_load_env_file_missing_is_noop(tmp_path: Path):
    load_env_file(tmp_path / "nope.env")  # must not raise
