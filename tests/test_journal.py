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
