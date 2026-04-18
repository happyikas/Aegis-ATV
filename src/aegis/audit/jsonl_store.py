"""Append-only JSONL store for raw signed audit records (PLAN 6.7)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class JsonlStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()

    def read_all(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if raw:
                    yield json.loads(raw)
