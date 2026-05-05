"""Trajectory cluster catalog (PR-ι, Phase B Tier 2).

Light-learning sibling to PR-ε (anomaly tags). Where PR-ε answers
"how anomalous is this on the time/IO axes?", PR-ι answers
"which prototypical burn-in pattern is this most similar to?"

Both feed the same narrative; together they give the sLLM both
**deviation** and **identity** signals.

Pipeline
--------

    TemporalContext  →  embed_trajectory()  →  ndarray (32-D)
                                                    ↓
              TrajectoryCatalog.nearest()  ←  k-means centroids
                                                    ↓
                  ("debug-error-spiral", cosine=0.78)
                                                    ↓
                      narrative tag injected by serialize_temporal()

Honest framing
--------------

The shipped default catalog is **synthetic** — eight hand-tuned
centroids covering common patterns (linear-edit-flow, debug-spiral,
context-saturated, etc.). It's not a learned model. Users who want
a personalised catalog run :func:`extract_catalog_from_sessions`
which:

* walks past Stop retrospectives + transcripts in parallel
* builds one trajectory embedding per session (last ``window_size``
  turns)
* runs pure-numpy k-means on the resulting matrix
* returns a :class:`TrajectoryCatalog`

We don't currently auto-label discovered clusters — they're tagged
``cluster_0`` … ``cluster_K-1``. PR-η (intent classifier) will add
soft labels when it lands.

Design properties
-----------------

* **Pure numpy** — no sklearn / scipy dependency
* **Sub-millisecond inference** — `.nearest()` is one matmul
* **Frozen, deterministic** — same trajectory → same nearest cluster
* **Audit-friendly JSON serialisation**
* **Cosine similarity** — direction-invariant, scale-stable
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from aegis.atv.temporal import TemporalContext


# ──────────────────────────────────────────────────────────────────────
# Trajectory embedding — fixed-shape feature vector
# ──────────────────────────────────────────────────────────────────────

# 32-D layout. Stable across versions; adding new features at the end
# keeps existing catalogs forward-compatible.
EMBEDDING_DIM: int = 32

# Bag-of-words slot for the most common Claude Code tool names. New
# tools land in the OOV bucket (slot 25 below). Bumping this list
# requires a catalog re-extraction.
_BOW_TOOLS: tuple[str, ...] = (
    "Read", "Edit", "Write", "MultiEdit",
    "Bash", "Grep", "Glob", "WebFetch",
    "TodoWrite", "WebSearch", "Task", "BashOutput",
    "NotebookEdit", "ExitPlanMode", "Agent", "(other)",
)
# 16 named tools → bow slots 9..24 (16 slots total). Slot 25 = OOV / Σ unknown.

# Normalisation constants — chosen to give all features ≈ unit-scale.
# Empirical from observed Claude Code sessions.
_TOKENS_PER_TURN_NORM = 2_000.0
_CUMULATIVE_TOKENS_NORM = 100_000.0


def embed_trajectory(ctx: TemporalContext) -> np.ndarray:
    """Build a fixed-size embedding from a TemporalContext.

    Layout (slots indexed from 0):
        0   mean cumulative_tokens delta (normalised)
        1   final cumulative_tokens (normalised)
        2   mean cache_hit_rate
        3   cache_hit_rate_max_drop_pp / 100
        4   n_backtracks / window_size
        5   n_redundant / window_size
        6   n_errors / window_size
        7   n_failures / window_size
        8   token_velocity_per_turn (normalised)
        9..24  bag-of-words for the 16 named tools (above)
        25  OOV tool count / window_size
        26  n_distinct_tools / 16
        27  success_ratio (1 − fail_ratio)
        28  is_progress_stalled flag
        29  cache_hit_rate_trajectory final value
        30  reserved (zero)
        31  reserved (zero)

    All values are bounded (mostly in [0, 1]) so cosine similarity
    behaves sensibly.
    """
    arr = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    n = len(ctx.history)
    window = max(ctx.window_size, n, 1)

    if n == 0:
        return arr

    # Token + cache trajectory derived stats
    cum = ctx.cumulative_token_trajectory
    if cum:
        # Mean per-turn delta, normalised
        if len(cum) >= 2:
            deltas = [cum[i] - cum[i - 1] for i in range(1, len(cum))]
            arr[0] = float(min(1.0, max(0.0,
                              (sum(deltas) / max(len(deltas), 1))
                              / _TOKENS_PER_TURN_NORM)))
        arr[1] = float(min(1.0, max(0.0,
                                    cum[-1] / _CUMULATIVE_TOKENS_NORM)))
    if ctx.cache_hit_rate_trajectory:
        arr[2] = float(np.mean(ctx.cache_hit_rate_trajectory))
        arr[29] = float(ctx.cache_hit_rate_trajectory[-1])

    arr[3] = float(min(1.0, max(0.0, ctx.cache_hit_rate_max_drop_pp / 100.0)))

    # Inefficiency signals normalised by window
    arr[4] = float(min(1.0, ctx.n_backtracks / window))
    arr[5] = float(min(1.0, ctx.n_redundant / window))
    arr[6] = float(min(1.0, ctx.n_errors / window))
    arr[7] = float(min(1.0, ctx.n_failures / window))

    # Token velocity
    arr[8] = float(min(1.0, max(0.0,
                                ctx.token_velocity_per_turn / _TOKENS_PER_TURN_NORM)))

    # Bag-of-words for tool names from history
    bow = np.zeros(len(_BOW_TOOLS), dtype=np.float32)
    n_oov = 0
    name_to_idx = {name: i for i, name in enumerate(_BOW_TOOLS)}
    for s in ctx.history:
        idx = name_to_idx.get(s.tool_name)
        if idx is None:
            n_oov += 1
        else:
            bow[idx] += 1.0
    if n > 0:
        bow = bow / float(n)              # → frequencies
    arr[9:9 + len(_BOW_TOOLS)] = bow
    arr[25] = float(min(1.0, n_oov / window))

    arr[26] = float(min(1.0, len(ctx.distinct_tools_in_window) / 16.0))

    # Outcome stats
    fail_n = sum(1 for s in ctx.history if s.outcome == "failure")
    arr[27] = float((n - fail_n) / max(n, 1))   # success ratio

    arr[28] = 1.0 if ctx.is_progress_stalled else 0.0

    return arr


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity (signed, in [-1, 1]). Returns 0 for zero
    vectors rather than NaN."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ──────────────────────────────────────────────────────────────────────
# Catalog dataclasses
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TrajectoryCluster:
    """One cluster centroid + summary metadata."""

    cluster_id: int
    label: str
    centroid: tuple[float, ...]              # length == EMBEDDING_DIM
    n_members: int
    success_ratio: float = 0.5
    notes: str = ""

    def centroid_array(self) -> np.ndarray:
        return np.asarray(self.centroid, dtype=np.float32)


@dataclass(frozen=True)
class TrajectoryCatalog:
    """Collection of cluster centroids derived from burn-in."""

    version: int
    embedding_dim: int
    n_burnin_trajectories: int
    extracted_at_ns: int
    extracted_from: str
    clusters: tuple[TrajectoryCluster, ...] = field(default_factory=tuple)
    notes: str = ""

    def is_usable(self) -> bool:
        return (
            len(self.clusters) >= 2
            and self.n_burnin_trajectories >= 8
            and self.embedding_dim == EMBEDDING_DIM
        )

    def nearest(
        self,
        embedding: np.ndarray,
        *,
        k: int = 1,
    ) -> list[tuple[TrajectoryCluster, float]]:
        """Top-k nearest clusters by cosine similarity, in descending
        order. Empty catalog → empty list."""
        if not self.clusters:
            return []
        scored: list[tuple[TrajectoryCluster, float]] = []
        for c in self.clusters:
            scored.append((c, _cosine(embedding, c.centroid_array())))
        scored.sort(key=lambda t: -t[1])
        return scored[:max(1, k)]


# ──────────────────────────────────────────────────────────────────────
# Default synthetic catalog
# ──────────────────────────────────────────────────────────────────────


def _centroid(slots: dict[int, float]) -> tuple[float, ...]:
    """Sparse centroid factory — only specify non-zero slots."""
    arr = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    for idx, value in slots.items():
        arr[idx] = value
    return tuple(arr.tolist())


def default_catalog() -> TrajectoryCatalog:
    """Synthetic 8-cluster catalog covering common Claude Code
    trajectory patterns. Hand-tuned centroids — no leaked user data.

    Operators with substantial real history should run
    :func:`extract_catalog_from_sessions` to get a personalised
    catalog. Until then, ``default_catalog()`` provides reasonable
    "nearest pattern" tagging.
    """
    bow = {name: i + 9 for i, name in enumerate(_BOW_TOOLS)}

    clusters = (
        TrajectoryCluster(
            cluster_id=0,
            label="linear-edit-flow",
            centroid=_centroid({
                0: 0.10, 1: 0.05, 2: 0.80, 27: 0.95,
                bow["Read"]: 0.30, bow["Edit"]: 0.30, bow["Grep"]: 0.20,
                26: 0.20,
            }),
            n_members=20,
            success_ratio=0.95,
            notes="Read → Edit → Grep, high cache, low signals",
        ),
        TrajectoryCluster(
            cluster_id=1,
            label="exploratory-search",
            centroid=_centroid({
                0: 0.10, 1: 0.10, 2: 0.40, 27: 0.90,
                bow["Read"]: 0.25, bow["Grep"]: 0.30, bow["Glob"]: 0.25,
                26: 0.30,
            }),
            n_members=15,
            success_ratio=0.92,
            notes="Read/Grep/Glob heavy, mid cache, high tool diversity",
        ),
        TrajectoryCluster(
            cluster_id=2,
            label="debug-error-spiral",
            centroid=_centroid({
                0: 0.40, 1: 0.30, 2: 0.30, 3: 0.30,
                4: 0.20, 6: 0.40,                    # backtrack + errors
                bow["Bash"]: 0.40, bow["Edit"]: 0.30,
                26: 0.15, 27: 0.30,                  # low success
                7: 0.40,
            }),
            n_members=10,
            success_ratio=0.30,
            notes="High error_rate + backtracks, Bash dominant",
        ),
        TrajectoryCluster(
            cluster_id=3,
            label="context-saturated",
            centroid=_centroid({
                0: 0.80, 1: 0.70, 2: 0.20, 3: 0.80,  # high token velocity, big cache drop
                8: 0.80,
                bow["Bash"]: 0.20, bow["Read"]: 0.30, bow["Edit"]: 0.20,
                26: 0.20, 27: 0.70,
            }),
            n_members=8,
            success_ratio=0.65,
            notes="Cache hit rate collapsed, high token velocity",
        ),
        TrajectoryCluster(
            cluster_id=4,
            label="redundant-loop",
            centroid=_centroid({
                0: 0.30, 1: 0.20, 2: 0.50,
                5: 0.50,                              # redundant
                bow["Bash"]: 0.40, bow["Grep"]: 0.30,
                26: 0.10, 27: 0.85,                   # narrow tool set
            }),
            n_members=8,
            success_ratio=0.85,
            notes="Same tool repeated, low diversity",
        ),
        TrajectoryCluster(
            cluster_id=5,
            label="test-cycle",
            centroid=_centroid({
                0: 0.20, 1: 0.10, 2: 0.70, 27: 0.85,
                bow["Bash"]: 0.40, bow["Edit"]: 0.20, bow["Read"]: 0.20,
                26: 0.20,
            }),
            n_members=12,
            success_ratio=0.85,
            notes="Bash pytest dominant, occasional Edit",
        ),
        TrajectoryCluster(
            cluster_id=6,
            label="init-cold",
            centroid=_centroid({
                0: 0.05, 1: 0.02, 2: 0.05, 27: 0.98,
                bow["Read"]: 0.40, bow["Glob"]: 0.20,
                26: 0.15,
            }),
            n_members=10,
            success_ratio=0.95,
            notes="Session start, cache empty, mostly Reads",
        ),
        TrajectoryCluster(
            cluster_id=7,
            label="general-default",
            centroid=_centroid({
                0: 0.20, 1: 0.15, 2: 0.50, 27: 0.85,
                bow["Read"]: 0.25, bow["Edit"]: 0.20, bow["Bash"]: 0.20,
                26: 0.25,
            }),
            n_members=17,
            success_ratio=0.85,
            notes="Mid-range pattern, no extremes",
        ),
    )

    return TrajectoryCatalog(
        version=1,
        embedding_dim=EMBEDDING_DIM,
        n_burnin_trajectories=sum(c.n_members for c in clusters),
        extracted_at_ns=0,
        extracted_from="synthetic-default",
        clusters=clusters,
        notes=(
            "Synthetic 8-cluster catalog. Replace with "
            "extract_catalog_from_sessions() once your audit + "
            "transcript pairs cover ≥ 50 sessions."
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# k-means (pure numpy)
# ──────────────────────────────────────────────────────────────────────


def _kmeans(
    samples: np.ndarray,
    *,
    k: int,
    max_iter: int = 50,
    seed: int = 1337,
) -> tuple[np.ndarray, np.ndarray]:
    """Pure-numpy spherical k-means. Deterministic given seed.

    Returns ``(centroids, labels)``. Samples are L2-normalised first
    so Euclidean k-means on the unit sphere ≈ spherical k-means
    (cosine clustering).
    """
    rng = np.random.default_rng(seed)
    if samples.shape[0] < k:
        # Not enough samples — return per-sample clusters.
        labels = np.arange(samples.shape[0])
        centroids = samples.copy()
        return centroids, labels

    # L2-normalise rows.
    norms = np.linalg.norm(samples, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    samples_n = samples / norms

    # k-means++ init
    n = samples_n.shape[0]
    init_idx = [int(rng.integers(0, n))]
    while len(init_idx) < k:
        c_used = samples_n[init_idx]
        d2 = np.min(
            np.linalg.norm(
                samples_n[:, None, :] - c_used[None, :, :], axis=2,
            ) ** 2,
            axis=1,
        )
        if d2.sum() <= 0:
            remaining = [i for i in range(n) if i not in init_idx]
            if not remaining:
                break
            init_idx.append(int(rng.choice(remaining)))
            continue
        probs = d2 / d2.sum()
        chosen = int(rng.choice(n, p=probs))
        init_idx.append(chosen)

    centroids = samples_n[init_idx].copy()

    for _ in range(max_iter):
        # Assign
        d = np.linalg.norm(
            samples_n[:, None, :] - centroids[None, :, :], axis=2,
        )
        labels = np.argmin(d, axis=1)
        # Update; re-seed empty clusters to a random sample.
        new_centroids = np.zeros_like(centroids)
        for i in range(k):
            members = samples_n[labels == i]
            if members.shape[0] == 0:
                new_centroids[i] = samples_n[rng.integers(0, n)]
            else:
                new_centroids[i] = members.mean(axis=0)
        nc = np.linalg.norm(new_centroids, axis=1, keepdims=True)
        nc[nc == 0] = 1.0
        new_centroids = new_centroids / nc
        if np.allclose(new_centroids, centroids, atol=1e-6):
            break
        centroids = new_centroids
    return centroids, labels


# ──────────────────────────────────────────────────────────────────────
# Extractor
# ──────────────────────────────────────────────────────────────────────


def extract_catalog_from_sessions(
    *,
    transcript_paths: list[Path],
    audit_path: Path,
    session_ids: list[str],
    n_clusters: int = 8,
    window_size: int = 5,
    notes: str = "",
) -> TrajectoryCatalog:
    """Build a catalog from past session data.

    For each (transcript_path, session_id) pair we run
    :func:`aegis.atv.temporal.load_recent_history` to get a
    TemporalContext, embed it via :func:`embed_trajectory`, then
    k-means cluster the resulting matrix.

    Empty / insufficient input → empty catalog with
    ``is_usable() == False``. Caller should fall back to
    :func:`default_catalog`.
    """
    from aegis.atv.temporal import load_recent_history

    if len(transcript_paths) != len(session_ids):
        raise ValueError(
            "transcript_paths and session_ids must be same length"
        )

    embeddings: list[np.ndarray] = []
    success_ratios: list[float] = []
    for ts_path, aid in zip(transcript_paths, session_ids, strict=True):
        try:
            ctx = load_recent_history(
                transcript_path=ts_path,
                audit_path=audit_path,
                session_id=aid,
                window_size=window_size,
            )
        except (OSError, ValueError):
            continue
        if len(ctx.history) == 0:
            continue
        emb = embed_trajectory(ctx)
        if np.linalg.norm(emb) == 0:
            continue
        embeddings.append(emb)
        # Success ratio for this trajectory
        n = len(ctx.history)
        n_fail = sum(1 for s in ctx.history if s.outcome == "failure")
        success_ratios.append((n - n_fail) / max(n, 1))

    if len(embeddings) < 8:
        # Not enough — return empty catalog so caller falls back to default.
        return TrajectoryCatalog(
            version=1, embedding_dim=EMBEDDING_DIM,
            n_burnin_trajectories=len(embeddings),
            extracted_at_ns=time.time_ns(),
            extracted_from=f"audit:{audit_path}",
            clusters=(),
            notes=(
                f"Too few sessions ({len(embeddings)}) for a "
                f"meaningful catalog. Use default_catalog() instead."
            ),
        )

    samples = np.stack(embeddings, axis=0)
    centroids, labels = _kmeans(samples, k=n_clusters)

    # Build clusters
    clusters: list[TrajectoryCluster] = []
    for cid in range(centroids.shape[0]):
        member_idx = [i for i, lab in enumerate(labels) if lab == cid]
        if not member_idx:
            continue
        member_success = [success_ratios[i] for i in member_idx]
        cluster_success = float(sum(member_success) / len(member_success))
        clusters.append(TrajectoryCluster(
            cluster_id=cid,
            label=f"cluster_{cid}",
            centroid=tuple(float(v) for v in centroids[cid]),
            n_members=len(member_idx),
            success_ratio=cluster_success,
            notes="auto-extracted (unlabelled)",
        ))

    return TrajectoryCatalog(
        version=1,
        embedding_dim=EMBEDDING_DIM,
        n_burnin_trajectories=len(embeddings),
        extracted_at_ns=time.time_ns(),
        extracted_from=f"audit:{audit_path}",
        clusters=tuple(clusters),
        notes=notes or (
            f"Extracted from {len(embeddings)} sessions; "
            f"{len(clusters)} non-empty clusters of {n_clusters} requested."
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


def catalog_to_dict(c: TrajectoryCatalog) -> dict[str, Any]:
    return {
        "version": c.version,
        "embedding_dim": c.embedding_dim,
        "n_burnin_trajectories": c.n_burnin_trajectories,
        "extracted_at_ns": c.extracted_at_ns,
        "extracted_from": c.extracted_from,
        "clusters": [asdict(cl) for cl in c.clusters],
        "notes": c.notes,
    }


def catalog_from_dict(d: dict[str, Any]) -> TrajectoryCatalog:
    clusters_raw = d.get("clusters") or []
    clusters: list[TrajectoryCluster] = []
    for cd in clusters_raw:
        if not isinstance(cd, dict):
            continue
        clusters.append(TrajectoryCluster(
            cluster_id=int(cd.get("cluster_id", 0)),
            label=str(cd.get("label", "cluster_unknown")),
            centroid=tuple(
                float(v) for v in (cd.get("centroid") or ())
            ),
            n_members=int(cd.get("n_members", 0)),
            success_ratio=float(cd.get("success_ratio", 0.5)),
            notes=str(cd.get("notes", "")),
        ))
    return TrajectoryCatalog(
        version=int(d.get("version", 1)),
        embedding_dim=int(d.get("embedding_dim", EMBEDDING_DIM)),
        n_burnin_trajectories=int(d.get("n_burnin_trajectories", 0)),
        extracted_at_ns=int(d.get("extracted_at_ns", 0)),
        extracted_from=str(d.get("extracted_from", "")),
        clusters=tuple(clusters),
        notes=str(d.get("notes", "")),
    )


def save_catalog(catalog: TrajectoryCatalog, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(catalog_to_dict(catalog), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_catalog(path: Path) -> TrajectoryCatalog:
    if not path.is_file():
        raise FileNotFoundError(f"trajectory catalog not at {path}")
    return catalog_from_dict(
        json.loads(path.read_text(encoding="utf-8"))
    )


def default_catalog_path() -> Path:
    """``AEGIS_TRAJECTORY_CATALOG_PATH`` env override → fallback to
    ``~/.aegis/trajectory_catalog.json`` → fallback to the shipped
    ``models/trajectory_catalog_v1.json``."""
    import os

    override = os.environ.get(
        "AEGIS_TRAJECTORY_CATALOG_PATH", "",
    ).strip()
    if override:
        return Path(override).expanduser()
    home_path = Path.home() / ".aegis" / "trajectory_catalog.json"
    if home_path.is_file():
        return home_path
    return (
        Path(__file__).resolve().parents[2].parents[0]
        / "models" / "trajectory_catalog_v1.json"
    )


def load_catalog_or_default(
    path: Path | None = None,
) -> TrajectoryCatalog:
    """Best-effort load: try the path, else fall back to
    :func:`default_catalog`. Never raises — narrative just omits
    the cluster section if everything fails."""
    p = path or default_catalog_path()
    try:
        cat = load_catalog(p)
        if cat.is_usable():
            return cat
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        pass
    return default_catalog()


# ──────────────────────────────────────────────────────────────────────
# Narrative renderer
# ──────────────────────────────────────────────────────────────────────


def render_nearest_clusters(
    ctx: TemporalContext,
    catalog: TrajectoryCatalog,
    *,
    k: int = 2,
) -> str:
    """Render a NEAREST BURN-IN PATTERN narrative section.

    Returns ``""`` when the catalog is unusable so the caller can
    skip the section entirely.
    """
    if not catalog.is_usable() or len(ctx.history) == 0:
        return ""

    emb = embed_trajectory(ctx)
    matches = catalog.nearest(emb, k=k)
    if not matches:
        return ""

    lines = ["NEAREST BURN-IN PATTERN"]
    primary, primary_sim = matches[0]
    lines.append(
        f"  most similar to: cluster #{primary.cluster_id} "
        f"'{primary.label}' (cosine {primary_sim:.2f}, "
        f"n={primary.n_members})"
    )
    lines.append(
        f"    historic success_ratio: "
        f"{primary.success_ratio:.0%}"
    )
    if primary.notes:
        lines.append(f"    pattern: {primary.notes}")
    if len(matches) >= 2:
        secondary, secondary_sim = matches[1]
        lines.append(
            f"  also similar to: cluster #{secondary.cluster_id} "
            f"'{secondary.label}' (cosine {secondary_sim:.2f})"
        )

    return "\n".join(lines)


def _stream_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Helper used by future extractors. Yields each well-formed JSON
    record from a JSONL file."""
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# Stable identifier for the embedding revision — bump on any layout
# change so audits can pin advices to a specific embedding version.
_EMBEDDING_VERSION = "trajectory_embedding_v1"
EMBEDDING_HASH: str = hashlib.sha3_256(
    _EMBEDDING_VERSION.encode()
).hexdigest()


__all__ = [
    "EMBEDDING_DIM",
    "EMBEDDING_HASH",
    "TrajectoryCatalog",
    "TrajectoryCluster",
    "catalog_from_dict",
    "catalog_to_dict",
    "default_catalog",
    "default_catalog_path",
    "embed_trajectory",
    "extract_catalog_from_sessions",
    "load_catalog",
    "load_catalog_or_default",
    "render_nearest_clusters",
    "save_catalog",
]
