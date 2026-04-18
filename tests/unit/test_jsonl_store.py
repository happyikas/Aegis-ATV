"""Tests for the append-only JSONL store."""

from __future__ import annotations

import threading
from pathlib import Path

from aegis.audit.jsonl_store import JsonlStore


def test_append_and_read(tmp_path: Path) -> None:
    s = JsonlStore(tmp_path / "audit.jsonl")
    s.append({"a": 1, "b": "x"})
    s.append({"a": 2, "b": "y"})
    items = list(s.read_all())
    assert items == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


def test_read_missing_file_returns_empty(tmp_path: Path) -> None:
    s = JsonlStore(tmp_path / "absent.jsonl")
    assert list(s.read_all()) == []


def test_concurrent_appends_dont_corrupt_lines(tmp_path: Path) -> None:
    s = JsonlStore(tmp_path / "concurrent.jsonl")
    n = 200

    def worker(start: int) -> None:
        for i in range(start, start + 20):
            s.append({"i": i})

    threads = [threading.Thread(target=worker, args=(i * 20,)) for i in range(n // 20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    items = list(s.read_all())
    assert len(items) == n
    assert sorted(it["i"] for it in items) == list(range(n))
