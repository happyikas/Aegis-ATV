"""Action embedding table (PR-κ, Phase B Tier 2).

Maps each Claude Code tool name (Read, Edit, Bash, Grep, ...) to a
small (16-D) vector in a shared semantic space. The embeddings
capture role-axes: read-vs-write, side-effect, exploration,
destructive-potential, testing, file-targeted, bulk, network, etc.

Use cases
---------

* :func:`action_similarity(a, b)` — cosine similarity. Read vs
  Grep are near (both read-only exploration); Edit vs Read are
  far (write-vs-read). Useful for "is this tool a sensible
  alternative to that one?"
* :func:`nearest_actions(query, k=3)` — given an action name or
  query embedding, return top-k similar action names. Drives
  the future ActionAdvice ``alternative_tool`` recommendations
  beyond the current hard-coded heuristics.
* :func:`embed_action_sequence(tools)` — mean-pool a tool sequence
  into one 16-D vector. Compact summary of recent activity for
  downstream learned heads (PR-ζ-head will use this as part of
  its context).

What this is NOT
----------------

Not a learned model — the default embedding table is hand-tuned
based on observed tool semantics. The shape (16-D, JSON-encoded
table) is forward-compatible with a learned-from-burn-in version
in a later PR (e.g., contrastive on tool-substitution patterns
in past sessions). For now the hand-tuned table provides a
deterministic baseline.

Honest framing
--------------

Hand-tuned ≠ data-derived. The numerical values reflect a
human's intuitions about tool roles, not a fitted distribution.
That intuition is encoded transparently in
:func:`default_action_table` so reviewers can spot disagreements
and the values are easy to retune.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# Embedding dimensionality. Adding new axes requires bumping
# TABLE_HASH so audits can pin to a specific revision.
ACTION_EMBEDDING_DIM: int = 16

# Axis legend (operator-readable; not enforced by code):
#   0  read-vs-write           (-1 read-only … +1 write-heavy)
#   1  side-effect-strength    (0 none … +1 spawns processes)
#   2  exploration-strength    (0 targeted … +1 broad search)
#   3  destructive-potential   (0 benign … +1 can break things)
#   4  testing-affinity        (+1 strongly testing-shaped)
#   5  file-targeted           (+1 names a specific file)
#   6  bulk-change             (+1 affects many files at once)
#   7  external-network        (+1 hits the network)
#   8  state-mutation          (+1 changes persistent state)
#   9  human-oversight-needed  (+1 typically reviewed)
#   10 navigation              (+1 changes working directory / scope)
#   11 introspection           (+1 reads agent's own state)
#   12 long-running            (+1 may take many seconds)
#   13 idempotent              (+1 safe to repeat)
#   14 reserved
#   15 reserved


_TABLE_VERSION = "action_embeddings_v1"
TABLE_HASH: str = hashlib.sha3_256(_TABLE_VERSION.encode()).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# Default hand-tuned embedding table
# ──────────────────────────────────────────────────────────────────────


def _vec(**axes: float) -> tuple[float, ...]:
    """Sparse vector factory — only specify named axes by index."""
    arr = [0.0] * ACTION_EMBEDDING_DIM
    axis_to_idx = {
        "read_write": 0,
        "side_effect": 1,
        "exploration": 2,
        "destructive": 3,
        "testing": 4,
        "file_targeted": 5,
        "bulk": 6,
        "network": 7,
        "state_mutation": 8,
        "human_review": 9,
        "navigation": 10,
        "introspection": 11,
        "long_running": 12,
        "idempotent": 13,
    }
    for name, value in axes.items():
        if name not in axis_to_idx:
            raise ValueError(
                f"unknown axis {name!r}; "
                f"known: {list(axis_to_idx)}"
            )
        arr[axis_to_idx[name]] = float(value)
    return tuple(arr)


def default_action_table() -> dict[str, tuple[float, ...]]:
    """Hand-tuned 16-D embeddings for each Claude Code tool.

    Edit values here to tune; bump :data:`TABLE_HASH` afterwards
    if downstream audits need to detect the change.
    """
    return {
        # Read-only file ops
        "Read": _vec(
            read_write=-0.9, file_targeted=0.9, idempotent=1.0,
            introspection=0.0,
        ),
        "Grep": _vec(
            read_write=-0.7, exploration=0.9, idempotent=1.0,
            file_targeted=0.3,
        ),
        "Glob": _vec(
            read_write=-0.6, exploration=1.0, idempotent=1.0,
            file_targeted=0.2,
        ),

        # Write file ops
        "Edit": _vec(
            read_write=0.8, file_targeted=0.9,
            state_mutation=0.7, human_review=0.5,
        ),
        "MultiEdit": _vec(
            read_write=0.9, file_targeted=0.5, bulk=0.9,
            state_mutation=0.8, human_review=0.7,
        ),
        "Write": _vec(
            read_write=1.0, file_targeted=0.9,
            state_mutation=0.9, human_review=0.6,
        ),
        "NotebookEdit": _vec(
            read_write=0.7, file_targeted=0.9,
            state_mutation=0.7, human_review=0.5,
        ),

        # Shell / process ops
        "Bash": _vec(
            read_write=0.3, side_effect=1.0, destructive=0.5,
            state_mutation=0.6, human_review=0.6,
            long_running=0.4,
        ),
        "BashOutput": _vec(
            read_write=-0.5, introspection=0.7, idempotent=1.0,
            long_running=0.3,
        ),

        # Network ops
        "WebFetch": _vec(
            read_write=-0.4, network=1.0, exploration=0.5,
            long_running=0.5, idempotent=0.7,
        ),
        "WebSearch": _vec(
            read_write=-0.3, network=0.9, exploration=0.9,
            idempotent=0.6,
        ),

        # Agent / orchestration
        "Task": _vec(
            read_write=0.0, side_effect=0.5, exploration=0.5,
            long_running=0.7,
        ),
        "Agent": _vec(
            read_write=0.0, side_effect=0.5, exploration=0.5,
            long_running=0.8,
        ),
        "TodoWrite": _vec(
            read_write=0.4, introspection=0.8, idempotent=0.7,
        ),
        "ExitPlanMode": _vec(
            navigation=1.0, introspection=0.5, idempotent=0.5,
        ),
    }


@dataclass(frozen=True)
class ActionTable:
    """Container for the action embedding table + provenance."""

    embedding_dim: int
    embeddings: dict[str, tuple[float, ...]]
    table_kind: str = "hand-tuned"
    table_hash: str = TABLE_HASH
    notes: str = ""

    def is_usable(self) -> bool:
        return (
            self.embedding_dim == ACTION_EMBEDDING_DIM
            and len(self.embeddings) >= 4
            and all(
                len(v) == ACTION_EMBEDDING_DIM
                for v in self.embeddings.values()
            )
        )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.embeddings.keys()))

    def matrix(self) -> tuple[tuple[str, ...], np.ndarray]:
        """Return (names, M) where M[i] is the embedding for names[i].
        Useful for batch operations."""
        ns = self.names()
        m = np.asarray(
            [self.embeddings[name] for name in ns], dtype=np.float32,
        )
        return ns, m


# ──────────────────────────────────────────────────────────────────────
# Default singleton
# ──────────────────────────────────────────────────────────────────────


def default_table() -> ActionTable:
    """The shipped hand-tuned table."""
    return ActionTable(
        embedding_dim=ACTION_EMBEDDING_DIM,
        embeddings=default_action_table(),
        table_kind="hand-tuned",
        table_hash=TABLE_HASH,
        notes=(
            "Hand-tuned 16-D embeddings for Claude Code tools. "
            "Axes documented in module docstring. "
            "Future: replace with contrastive-trained embeddings "
            "from burn-in tool-substitution patterns."
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Lookup primitives
# ──────────────────────────────────────────────────────────────────────


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity (signed, in [-1, 1]). Returns 0 for zero
    vectors rather than NaN."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def embed_action(
    name: str, table: ActionTable | None = None,
) -> np.ndarray:
    """Look up the embedding for a single action name. Unknown
    actions return a zero vector — caller can detect via
    ``np.linalg.norm(emb) == 0``."""
    t = table or default_table()
    vec = t.embeddings.get(name)
    if vec is None:
        return np.zeros(t.embedding_dim, dtype=np.float32)
    return np.asarray(vec, dtype=np.float32)


def action_similarity(
    a: str, b: str, table: ActionTable | None = None,
) -> float:
    """Cosine similarity between two action names. Unknown name on
    either side → 0."""
    t = table or default_table()
    va = embed_action(a, t)
    vb = embed_action(b, t)
    return _cosine(va, vb)


def nearest_actions(
    query: str | np.ndarray,
    *,
    k: int = 3,
    table: ActionTable | None = None,
    exclude_self: bool = True,
) -> list[tuple[str, float]]:
    """Top-k nearest actions to ``query`` by cosine similarity.

    ``query`` may be:
    * an action name (in which case its own row is excluded when
      ``exclude_self=True``)
    * a numpy embedding vector

    Returns ``[(name, similarity), ...]`` sorted descending. Empty
    table → empty list.
    """
    t = table or default_table()
    if not t.is_usable():
        return []

    if isinstance(query, str):
        q = embed_action(query, t)
        self_name = query
    else:
        q = np.asarray(query, dtype=np.float32)
        if q.shape != (t.embedding_dim,):
            raise ValueError(
                f"query embedding shape mismatch: "
                f"expected ({t.embedding_dim},), got {q.shape}"
            )
        self_name = None

    if float(np.linalg.norm(q)) == 0.0:
        return []

    names, m = t.matrix()
    # Cosine row-by-row.
    norms = np.linalg.norm(m, axis=1)
    norms_q = float(np.linalg.norm(q))
    norms_safe = np.where(norms == 0.0, 1.0, norms)
    sims = (m @ q) / (norms_safe * norms_q)

    results: list[tuple[str, float]] = []
    for name, sim in zip(names, sims, strict=True):
        if exclude_self and name == self_name:
            continue
        results.append((name, float(sim)))
    results.sort(key=lambda t: -t[1])
    return results[:max(1, k)]


def embed_action_sequence(
    tool_names: list[str],
    *,
    table: ActionTable | None = None,
) -> np.ndarray:
    """Mean-pooled embedding over a tool-name sequence.

    Useful for downstream learned heads that want a compact
    summary of "what kind of tools the agent has been using
    recently". Unknown tools contribute zero vectors (so they
    drag the mean toward zero rather than crashing).

    Empty sequence → zero vector.
    """
    t = table or default_table()
    if not tool_names:
        return np.zeros(t.embedding_dim, dtype=np.float32)
    vecs = [embed_action(n, t) for n in tool_names]
    stacked = np.stack(vecs, axis=0)
    out: np.ndarray = stacked.mean(axis=0).astype(np.float32)
    return out


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


def table_to_dict(table: ActionTable) -> dict[str, Any]:
    return {
        "embedding_dim": table.embedding_dim,
        "embeddings": {
            name: list(vec)
            for name, vec in sorted(table.embeddings.items())
        },
        "table_kind": table.table_kind,
        "table_hash": table.table_hash,
        "notes": table.notes,
    }


def table_from_dict(d: dict[str, Any]) -> ActionTable:
    embs_raw = d.get("embeddings") or {}
    embeddings: dict[str, tuple[float, ...]] = {}
    for name, vec in embs_raw.items():
        if not isinstance(vec, (list, tuple)):
            continue
        embeddings[str(name)] = tuple(float(v) for v in vec)
    return ActionTable(
        embedding_dim=int(d.get("embedding_dim", ACTION_EMBEDDING_DIM)),
        embeddings=embeddings,
        table_kind=str(d.get("table_kind", "hand-tuned")),
        table_hash=str(d.get("table_hash", TABLE_HASH)),
        notes=str(d.get("notes", "")),
    )


def save_table(table: ActionTable, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(table_to_dict(table), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_table(path: Path) -> ActionTable:
    if not path.is_file():
        raise FileNotFoundError(
            f"action embedding table not at {path}"
        )
    return table_from_dict(json.loads(path.read_text(encoding="utf-8")))


def default_table_path() -> Path:
    """Honours ``AEGIS_ACTION_TABLE_PATH`` env override; falls back
    to ``~/.aegis/action_embeddings.json``; final fallback to the
    shipped ``models/action_embeddings_v1.json``."""
    import os

    override = os.environ.get(
        "AEGIS_ACTION_TABLE_PATH", "",
    ).strip()
    if override:
        return Path(override).expanduser()
    home = Path.home() / ".aegis" / "action_embeddings.json"
    if home.is_file():
        return home
    return (
        Path(__file__).resolve().parents[2].parents[0]
        / "models" / "action_embeddings_v1.json"
    )


def load_table_or_default(
    path: Path | None = None,
) -> ActionTable:
    """Best-effort load → fall back to :func:`default_table`. Never
    raises."""
    p = path or default_table_path()
    try:
        t = load_table(p)
        if t.is_usable():
            return t
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        pass
    return default_table()


__all__ = [
    "ACTION_EMBEDDING_DIM",
    "ActionTable",
    "TABLE_HASH",
    "action_similarity",
    "default_action_table",
    "default_table",
    "default_table_path",
    "embed_action",
    "embed_action_sequence",
    "load_table",
    "load_table_or_default",
    "nearest_actions",
    "save_table",
    "table_from_dict",
    "table_to_dict",
]
