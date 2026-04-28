"""Instruction-baseline data model + capture / verify (v2.2.1).

The baseline is a JSON manifest the user reviews and signs off on:

.. code-block:: json

    {
      "version": 1,
      "created_at_ns": 1745000000000000000,
      "root": "/abs/path/to/repo",
      "files": {
        "CLAUDE.md":         "<sha3_256 hex>",
        "AGENTS.md":         "<sha3_256 hex>",
        ".mcp.json":         "<sha3_256 hex>",
        ".claude-plugin/plugin.json": "<sha3_256 hex>"
      }
    }

It lives next to the repo at ``.aegis/instruction_baseline.json``.
``snapshot(root)`` walks ``DEFAULT_INSTRUCTION_PATHS`` (plus any extra
glob patterns) and produces an :class:`InstructionBaseline`.
``diff_baseline(baseline, root)`` re-hashes the same set on the fly
and returns a :class:`DriftReport` of added / removed / modified
files.

The hashing is plain SHA3-256 over the file contents — no canonical
serialisation needed because instruction files are line-oriented text
the user authored. A whitespace change is treated as drift on
purpose: poisoning often hides in trailing-whitespace-only diffs.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

# v2.2: the canonical instruction-surface set. Glob patterns relative
# to the repo root, evaluated by Path.glob.
DEFAULT_INSTRUCTION_PATHS: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    ".mcp.json",
    ".claude-plugin/plugin.json",
    ".claude/skills/*.md",
    ".claude/commands/*.md",
    ".cursor/rules/*.mdc",
)


def hash_file(path: Path) -> str:
    """Return SHA3-256 hex of the file's contents."""
    h = hashlib.sha3_256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class InstructionBaseline:
    """Snapshot of every instruction file's hash at a point in time."""

    version: int
    created_at_ns: int
    root: str
    files: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "created_at_ns": self.created_at_ns,
            "root": self.root,
            "files": dict(self.files),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> InstructionBaseline:
        files_raw = data.get("files") or {}
        if not isinstance(files_raw, dict):
            raise ValueError("baseline 'files' must be a dict")
        version_raw = data.get("version", 1)
        created_raw = data.get("created_at_ns", 0)
        return cls(
            version=int(version_raw) if isinstance(version_raw, (int, str)) else 1,
            created_at_ns=int(created_raw)
            if isinstance(created_raw, (int, str))
            else 0,
            root=str(data.get("root", "")),
            files={str(k): str(v) for k, v in files_raw.items()},
        )


@dataclass(frozen=True)
class DriftReport:
    """Result of comparing a live tree against a baseline.

    All path lists are relative to ``root``. ``modified`` carries
    ``(path, baseline_hash, current_hash)`` so callers can render a
    short diff summary in a block message.
    """

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def summary(self) -> str:
        bits: list[str] = []
        if self.added:
            bits.append(f"+{len(self.added)} added")
        if self.removed:
            bits.append(f"-{len(self.removed)} removed")
        if self.modified:
            bits.append(f"~{len(self.modified)} modified")
        return ", ".join(bits) or "no drift"


def _resolve_paths(
    root: Path, patterns: tuple[str, ...] | list[str]
) -> list[Path]:
    """Resolve glob patterns under ``root`` to existing file paths.

    Plain (non-glob) patterns are accepted as direct file paths. The
    result is sorted so the manifest is deterministic.
    """
    seen: set[Path] = set()
    for pattern in patterns:
        if any(c in pattern for c in "*?[]"):
            # Glob — relative to root.
            for p in root.glob(pattern):
                if p.is_file():
                    seen.add(p)
        else:
            p = root / pattern
            if p.is_file():
                seen.add(p)
    return sorted(seen)


def snapshot(
    root: Path,
    *,
    patterns: tuple[str, ...] | list[str] = DEFAULT_INSTRUCTION_PATHS,
) -> InstructionBaseline:
    """Walk ``patterns`` under ``root`` and produce an InstructionBaseline."""
    root = root.resolve()
    files: dict[str, str] = {}
    for path in _resolve_paths(root, patterns):
        rel = path.relative_to(root).as_posix()
        files[rel] = hash_file(path)
    return InstructionBaseline(
        version=1,
        created_at_ns=time.time_ns(),
        root=str(root),
        files=files,
    )


def diff_baseline(
    baseline: InstructionBaseline,
    root: Path,
    *,
    patterns: tuple[str, ...] | list[str] | None = None,
) -> DriftReport:
    """Compare the live tree under ``root`` against ``baseline.files``.

    By default we walk the keys IN the baseline plus the
    DEFAULT_INSTRUCTION_PATHS — that way a brand-new ``AGENTS.md`` is
    flagged as 'added' even if it wasn't in the baseline at all.
    """
    root = root.resolve()
    if patterns is None:
        # Build a combined set: paths in the baseline (so removals are
        # detected) ∪ default discovery patterns (so additions like a
        # new file matching a glob are detected).
        explicit: list[str] = list(baseline.files.keys())
        explicit.extend(DEFAULT_INSTRUCTION_PATHS)
        patterns = tuple(dict.fromkeys(explicit))  # de-dupe, keep order

    live_paths = _resolve_paths(root, patterns)
    live_hashes: dict[str, str] = {}
    for path in live_paths:
        rel = path.relative_to(root).as_posix()
        # If a path was specified explicitly (non-glob), compare regardless;
        # if matched only by glob, still include — we want every poisoned
        # source to show up.
        live_hashes[rel] = hash_file(path)

    # Also explicitly check baseline-keyed files even if they don't match
    # the glob set (e.g. baseline tracked a custom file the default
    # patterns don't cover).
    for rel in baseline.files:
        if rel in live_hashes:
            continue
        candidate = root / rel
        if candidate.is_file():
            live_hashes[rel] = hash_file(candidate)
        # else: missing → handled below as "removed"

    added: list[str] = []
    removed: list[str] = []
    modified: list[tuple[str, str, str]] = []

    baseline_keys = set(baseline.files.keys())
    live_keys = set(live_hashes.keys())

    for rel in sorted(live_keys - baseline_keys):
        added.append(rel)
    for rel in sorted(baseline_keys - live_keys):
        removed.append(rel)
    for rel in sorted(baseline_keys & live_keys):
        if baseline.files[rel] != live_hashes[rel]:
            modified.append((rel, baseline.files[rel], live_hashes[rel]))

    # Filter glob-discovered fnmatches that the user may not care about.
    # Currently we surface everything; this is a placeholder for future
    # selective suppression (e.g. .claude/skills/*.md churn during dev).
    _ = fnmatch  # silence unused import (kept for future filtering)

    return DriftReport(added=added, removed=removed, modified=modified)


def write_baseline(baseline: InstructionBaseline, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(baseline.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_baseline(path: Path) -> InstructionBaseline:
    if not path.exists():
        raise FileNotFoundError(f"no instruction baseline at {path}")
    return InstructionBaseline.from_dict(json.loads(path.read_text(encoding="utf-8")))
