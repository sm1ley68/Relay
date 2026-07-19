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


def test_estimate_file_count_scoped_to_py_and_excludes_vcs(tmp_path: Path):
    (tmp_path / "billing.py").write_text("x = 1\n")
    (tmp_path / "billing.txt").write_text("not python\n")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "billing.py").write_text("x = 1\n")
    n = estimate_file_count("touch billing", tmp_path)
    assert n == 1
