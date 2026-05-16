"""Filesystem store for ContextMemory knowledge entries (v0.5.15).

One JSON file per entry, written atomically (tempfile + rename),
plus a small ``index.json`` for fast catalog enumeration. The
layout mirrors the autonomy trust table convention:

  ~/.aegis/knowledge/
    index.json                  ← entry catalog (entry_id → metadata)
    agent_foo.json
    tool_Bash.json
    pattern_loop_Bash.json
    ...

Why one file per entry rather than one big JSONL?

* **Selective load** — the renderer fetches just the entries the
  sLLM advisor needs (typically ~10), not the whole catalog.
  One-file-per-entry makes the fetch O(entries) not O(corpus).
* **Git-diff friendly** — each entry can be reviewed in isolation
  if the operator commits the knowledge base alongside their
  project.
* **Atomic rewrites** — building one entry only writes one file;
  a partial build doesn't leave the whole catalog inconsistent.
* **Schema evolution** — the file path encodes the kind, so a
  future schema bump can rename the directory without re-parsing
  every file to figure out what's in it.

The ``index.json`` is the catalog. It's the only file the ``list``
operation needs to read, and lets the retrieval helper enumerate
entries without globbing the directory.

All paths honour ``AEGIS_KNOWLEDGE_DIR`` for tests + multi-tenant
deployments. Defaults to ``~/.aegis/knowledge``."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from aegis.knowledge.schema import (
    EntryKind,
    KnowledgeEntry,
    make_entry_id,
    split_entry_id,
)

# ──────────────────────────────────────────────────────────────────
# Path resolution
# ──────────────────────────────────────────────────────────────────


def knowledge_dir() -> Path:
    """Return the canonical knowledge directory.

    Honours ``AEGIS_KNOWLEDGE_DIR`` for tests / multi-tenant
    deployments; defaults to ``~/.aegis/knowledge``."""
    raw = os.environ.get("AEGIS_KNOWLEDGE_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".aegis" / "knowledge"


def index_path(root: Path | None = None) -> Path:
    """Path of the catalog index file inside the knowledge dir."""
    base = root if root is not None else knowledge_dir()
    return base / "index.json"


# ──────────────────────────────────────────────────────────────────
# Entry path encoding
# ──────────────────────────────────────────────────────────────────

# Allow letters, digits, dot, hyphen, underscore. Anything else
# gets replaced with underscore so the filename is portable.
_SLUG_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _entry_filename(entry_id: str) -> str:
    """Encode an entry_id into a filesystem-safe filename.

    Examples:
      ``agent/foo``          → ``agent_foo.json``
      ``tool/Bash``          → ``tool_Bash.json``
      ``pattern/loop:Bash``  → ``pattern_loop_Bash.json``

    Note this is *not* perfectly reversible (colons / slashes
    in slugs collide with underscores in slugs). The reverse
    direction goes through the index.json which preserves the
    canonical entry_id."""
    kind, slug = split_entry_id(entry_id)
    safe_slug = _SLUG_SAFE_RE.sub("_", slug)
    return f"{kind.value}_{safe_slug}.json"


def entry_path(entry_id: str, root: Path | None = None) -> Path:
    """Resolve the on-disk path for an entry."""
    base = root if root is not None else knowledge_dir()
    return base / _entry_filename(entry_id)


# ──────────────────────────────────────────────────────────────────
# Index data structures
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IndexEntry:
    """One row of the catalog index. Compact metadata for list /
    filter operations without loading the full entry body."""

    entry_id: str
    kind: EntryKind
    title: str
    summary: str
    tags: tuple[str, ...] = field(default_factory=tuple)
    ts_last_ns: int = 0
    n_observations: int = 0
    confidence: float = 1.0
    filename: str = ""


# ──────────────────────────────────────────────────────────────────
# Atomic write
# ──────────────────────────────────────────────────────────────────


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    """Tempfile + rename, so a concurrent reader never sees a
    partial JSON document. Same pattern as the autonomy trust
    table writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


# ──────────────────────────────────────────────────────────────────
# Save + load
# ──────────────────────────────────────────────────────────────────


def save_entry(
    entry: KnowledgeEntry, *, root: Path | None = None,
) -> Path:
    """Persist one entry. Returns the path written. Does NOT
    update the catalog index — the caller batches index updates
    via :func:`save_index` after writing all entries (this keeps
    a build pass atomic even if it writes hundreds of entries)."""
    base = root if root is not None else knowledge_dir()
    path = entry_path(entry.entry_id, root=base)
    _write_atomic(path, entry.to_dict())
    return path


def load_entry(
    entry_id: str, *, root: Path | None = None,
) -> KnowledgeEntry | None:
    """Read one entry by entry_id. Returns ``None`` if the file
    doesn't exist or is unparseable — the retrieval layer treats
    this as "no entry" rather than raising, so a stale cross-
    reference never crashes the sLLM advisor."""
    base = root if root is not None else knowledge_dir()
    path = entry_path(entry_id, root=base)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return KnowledgeEntry.from_dict(payload)


def save_index(
    entries: list[KnowledgeEntry],
    *,
    root: Path | None = None,
    built_at_ns: int = 0,
    built_from_records: int = 0,
) -> Path:
    """Write the catalog index from the just-built entries.

    The index is the *only* file the ``list`` operation reads,
    so building it correctly is the difference between a fast
    catalog scan and a directory walk that opens every file."""
    base = root if root is not None else knowledge_dir()
    rows = [
        {
            "entry_id": e.entry_id,
            "kind": e.kind.value,
            "title": e.title,
            "summary": e.summary,
            "tags": list(e.tags),
            "ts_last_ns": e.ts_last_ns,
            "n_observations": e.n_observations,
            "confidence": e.confidence,
            "filename": _entry_filename(e.entry_id),
        }
        for e in entries
    ]
    payload: dict[str, object] = {
        "schema_version": 1,
        "built_at_ns": built_at_ns,
        "built_from_records": built_from_records,
        "entries": rows,
    }
    target = index_path(base)
    _write_atomic(target, payload)
    return target


def load_index(
    *, root: Path | None = None,
) -> list[IndexEntry]:
    """Read the catalog. Returns an empty list on missing /
    malformed index — same defensive contract as the entry
    loader."""
    base = root if root is not None else knowledge_dir()
    target = index_path(base)
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("entries", [])
    if not isinstance(rows, list):
        return []
    out: list[IndexEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            kind = EntryKind(row.get("kind", "agent"))
        except ValueError:
            continue
        out.append(IndexEntry(
            entry_id=str(row.get("entry_id", "")),
            kind=kind,
            title=str(row.get("title", "")),
            summary=str(row.get("summary", "")),
            tags=tuple(str(x) for x in row.get("tags", []) or ()),
            ts_last_ns=int(row.get("ts_last_ns", 0) or 0),
            n_observations=int(row.get("n_observations", 0) or 0),
            confidence=float(row.get("confidence", 1.0) or 1.0),
            filename=str(row.get("filename", "")),
        ))
    return out


def index_metadata(
    *, root: Path | None = None,
) -> dict[str, object]:
    """Return the index's top-level metadata (when it was built,
    from how many records). Empty dict on missing file."""
    base = root if root is not None else knowledge_dir()
    target = index_path(base)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        k: payload.get(k)
        for k in ("schema_version", "built_at_ns", "built_from_records")
    }


# Reverse alias — useful for tests that want to verify the
# canonical entry-id given a kind + slug pair without depending
# on the schema module directly.
_make_entry_id = make_entry_id


__all__ = [
    "IndexEntry",
    "entry_path",
    "index_metadata",
    "index_path",
    "knowledge_dir",
    "load_entry",
    "load_index",
    "save_entry",
    "save_index",
]
