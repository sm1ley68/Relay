import json
from pathlib import Path
from orchestrator.budget import ProWindow


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


def test_pro_window_degrades_on_non_iterable_json(tmp_path: Path):
    p = tmp_path / "budget.json"
    p.write_text("null")
    w = ProWindow(p, 5.0, 5)
    assert w.runs_in_window() == 0
    w.record_run()


def test_pro_window_prunes_expired_stamps_on_write(tmp_path: Path):
    # the ledger must stay bounded instead of growing for the life of the install
    p = tmp_path / "budget.json"
    w = ProWindow(p, window_hours=1.0, max_runs=100)
    for i in range(20):
        w.record_run(1000.0 + i)
    assert len(json.loads(p.read_text())) == 20

    w.record_run(1000.0 + 20 + 3600)  # an hour later: all the old ones expired
    assert json.loads(p.read_text()) == [1000.0 + 20 + 3600]


def test_pro_window_remaining_and_reset(tmp_path: Path):
    w = ProWindow(tmp_path / "budget.json", window_hours=5.0, max_runs=3)
    now = 1000.0
    assert w.remaining(now) == 3
    w.record_run(now)
    assert w.remaining(now) == 2
    # the oldest run ages out exactly one window after it was recorded
    assert w.resets_in_seconds(now) == 5 * 3600


def test_pro_window_write_is_atomic_no_partial_file(tmp_path: Path):
    p = tmp_path / "budget.json"
    w = ProWindow(p, 5.0, 5)
    w.record_run(1000.0)
    # no stray temp files left behind by the atomic replace
    assert [f.name for f in tmp_path.iterdir()] == ["budget.json"]
    assert json.loads(p.read_text()) == [1000.0]
