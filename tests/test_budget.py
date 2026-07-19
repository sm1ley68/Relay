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

    # 5h + 1s after the *last* recorded run, both fall out of the window
    # (using `now` here instead of `now + 10` would leave the second run
    # inside the window, since it was recorded 10s after `now`)
    later = (now + 10) + 5 * 3600 + 1
    assert w.runs_in_window(later) == 0
    assert w.can_run(later) is True


def test_pro_window_persists_across_instances(tmp_path: Path):
    p = tmp_path / "budget.json"
    ProWindow(p, 5.0, 5).record_run(1000.0)
    assert ProWindow(p, 5.0, 5).runs_in_window(1001.0) == 1
