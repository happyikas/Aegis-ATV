"""KV cache advisory head — pure function ATV → KVCacheAdvice.

The advisor reads the same 2080-D ATV the trust firewall uses and emits
hints about how a downstream LLM serving runtime should manage its
paged KV cache for the *next* turn. The runtime is the enforcer; Aegis
is only the advisor.

Read these subfields (out of the 30) — they carry perf-relevant signal:

* ``cost_efficiency_metrics`` (16 slots) — `cache_hit_rate` (s-10),
  `context_utilization_ratio` (s-11), `cumulative_tokens` (s-4),
  `task_progress_score` (s-15) → residency tier + speculative
* ``action_history`` (640) + ``action_blast_radius`` (16) → batch_key
  derivation: same agent state + same blast envelope → same KV layout
  → batchable with peer
* ``inter_agent_graph`` (128) → cross-agent shared KV segments
  (when two agents observe the same upstream conversation, the prefix
  can be shared)
* ``novelty_score`` (4) — high novelty → cold path; low → hot path
* ``prompt_structure`` (16) — code-block + length proxy → speculative
  decoding candidate
* ``memory_provenance`` (64) → segment IDs (deterministic SHA3 over
  fingerprint) for prefetch_segment_ids

Design properties (patent-relevant)
-----------------------------------
1. **Advisory only** — KVCacheAdvice is a hint, never a directive.
   The runtime is free to ignore it. This decouples the patent from
   any specific runtime's API.
2. **Pure function** — `kv_cache_advisor(atv, inp) → KVCacheAdvice`.
   No I/O, no global state, deterministic. Same ATV → same advice
   (audit-friendly).
3. **Sub-millisecond** — same numpy slicing M13 already does.
4. **Closed loop** — v3.2 adds `cache_hit_rate` from the runtime's
   `/tool-outcome` callback so s-10 of the next ATV reflects reality.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from aegis.schema import (
    SLICE_ACTION_BLAST_RADIUS,
    SLICE_ACTION_HISTORY,
    SLICE_AGENT_STATE_EMBEDDING,
    SLICE_COST_EFFICIENCY_METRICS,
    SLICE_INTER_AGENT_GRAPH,
    SLICE_MEMORY_PROVENANCE,
    SLICE_NOVELTY_SCORE,
    SLICE_PROMPT_STRUCTURE,
    ATVInput,
)

# Indices within the 16-D cost_efficiency_metrics slice
_COST_S4_CUMULATIVE_TOKENS = 3
_COST_S10_CACHE_HIT_RATE = 9
_COST_S11_CONTEXT_UTILIZATION = 10
_COST_S15_TASK_PROGRESS = 14

ResidencyClass = Literal["hot", "warm", "cold"]


@dataclass(frozen=True)
class KVCacheAdvice:
    """Out-of-band advisory payload — runtime decides whether to honour.

    Attributes
    ----------
    prefetch_segment_ids:
        Stable string IDs of KV segments the runtime should bring into
        HBM before the next decode step. Derived deterministically from
        memory_provenance + inter_agent_graph hashes.
    evict_candidates:
        IDs of segments that are safe to demote (CPU RAM / SSD). Empty
        when the advisor has no eviction signal.
    residency_class:
        Suggested tier for the *current* conversation's working set.
        ``hot`` (HBM-resident, prefetch aggressive), ``warm`` (HBM
        with normal LRU), ``cold`` (eligible for eviction).
    batch_key:
        Cohort identifier — runtime can batch peers with the same
        ``batch_key`` together. Built from the agent_state +
        blast_radius fingerprint, so peers in the same task phase
        line up.
    speculative_decode:
        Whether the next decode is a good candidate for speculative
        decoding (low-novelty + structured prompt → yes).
    confidence:
        Self-reported advisor confidence in [0, 1]. Runtime can fall
        back to its own heuristics below a threshold.
    reasons:
        Human-readable trace of which subfields drove the advice.
    latency_ms:
        Wall-clock the advisor took.
    advisor_hash:
        SHA3-256 of the advisor implementation version (for audit).
    """

    prefetch_segment_ids: list[str] = field(default_factory=list)
    evict_candidates: list[str] = field(default_factory=list)
    residency_class: ResidencyClass = "warm"
    batch_key: str = ""
    speculative_decode: bool = False
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    advisor_hash: str = ""


# Frozen advisor logic version. Bump on any change to the formulas
# below so the audit can pin advice to a specific advisor revision.
_ADVISOR_VERSION = "kv_cache_advisor_v1"
_ADVISOR_HASH = hashlib.sha3_256(_ADVISOR_VERSION.encode()).hexdigest()


def _segment_ids_from_band(band: np.ndarray, count: int, *, prefix: str) -> list[str]:
    """Deterministic segment-ID extraction from a hash-expanded band.

    The HASH-EXPAND encoders (memory_provenance, inter_agent_graph)
    produce SHA3-512 expanded float32 vectors. We re-hash the *bytes*
    to obtain stable string IDs the runtime can use as KV-segment
    cache keys.
    """
    if band.size == 0 or count <= 0:
        return []
    raw = band.tobytes()
    digest = hashlib.sha3_256(raw).digest()
    out: list[str] = []
    for i in range(count):
        chunk = digest[i * 4 : (i + 1) * 4]
        if len(chunk) < 4:
            digest = hashlib.sha3_256(digest).digest()
            chunk = digest[:4]
        out.append(f"{prefix}-{chunk.hex()}")
    return out


def _stable_batch_key(agent_emb: np.ndarray, blast: np.ndarray) -> str:
    """Cohort key — shared by peers with same state + same blast envelope.

    Quantises the floats to 1e-2 buckets before hashing so small
    drift doesn't shatter the cohort. Returns an 8-byte hex prefix.
    """
    quantised = np.concatenate([
        np.round(agent_emb * 100.0).astype(np.int32),
        np.round(blast * 100.0).astype(np.int32),
    ]).tobytes()
    return hashlib.sha3_256(quantised).hexdigest()[:16]


def _residency_from_signals(
    cache_hit_rate: float,
    novelty: float,
    progress: float,
    context_util: float,
) -> tuple[ResidencyClass, list[str]]:
    """Decide hot / warm / cold from the cost+novelty signals.

    Heuristic, hand-tuned in v3.1. v3.6 will replace with a learned
    head sharing M13's structure.
    """
    reasons: list[str] = []
    # high progress + low novelty = re-visit likely → hot
    if progress >= 0.30 and novelty < 0.30:
        reasons.append(
            f"hot: task_progress={progress:.2f} ≥ 0.30, novelty={novelty:.2f} < 0.30"
        )
        return "hot", reasons
    # low cache hits + high context util = working set fits poorly → cold
    if cache_hit_rate < 0.20 and context_util >= 0.70:
        reasons.append(
            f"cold: cache_hit_rate={cache_hit_rate:.2f} < 0.20, "
            f"context_util={context_util:.2f} ≥ 0.70"
        )
        return "cold", reasons
    reasons.append(
        f"warm: cache_hit_rate={cache_hit_rate:.2f}, novelty={novelty:.2f}, "
        f"task_progress={progress:.2f}"
    )
    return "warm", reasons


def _speculative_eligible(prompt_struct: np.ndarray, novelty: float) -> tuple[bool, str]:
    """Speculative decoding works best on low-entropy, structured input."""
    if prompt_struct.size < 7:
        return False, "speculative=False: prompt_structure too short"
    has_code_block = float(prompt_struct[6]) > 0.5  # idx 6 = "```" presence
    length_norm = float(prompt_struct[0])           # idx 0 = length / 4000
    if novelty > 0.50:
        return False, f"speculative=False: novelty={novelty:.2f} > 0.50"
    if has_code_block and length_norm > 0.10:
        return True, "speculative=True: code-block prompt, low novelty"
    if length_norm > 0.50 and novelty < 0.20:
        return True, "speculative=True: long-but-stable prompt"
    return False, "speculative=False: no positive signals"


def kv_cache_advisor(
    atv: np.ndarray,
    inp: ATVInput | None = None,
) -> KVCacheAdvice:
    """Pure function: 2080-D ATV → KVCacheAdvice.

    Parameters
    ----------
    atv:
        The full 2080-D float32 vector built by ``aegis.atv.builder.build_atv``.
    inp:
        Optional :class:`ATVInput` — used only for additional context
        (currently unused inside the advisor itself, reserved for
        v3.4+ scheduling decisions).

    Returns
    -------
    KVCacheAdvice (immutable dataclass).
    """
    t0 = time.perf_counter_ns()

    cost = atv[SLICE_COST_EFFICIENCY_METRICS]
    cache_hit_rate = float(cost[_COST_S10_CACHE_HIT_RATE])
    context_util = float(cost[_COST_S11_CONTEXT_UTILIZATION])
    progress = float(cost[_COST_S15_TASK_PROGRESS])
    cum_tokens = float(cost[_COST_S4_CUMULATIVE_TOKENS])

    novelty_band = atv[SLICE_NOVELTY_SCORE]
    composite_novelty = float(novelty_band[3]) if novelty_band.size >= 4 else 0.0

    prompt_struct = atv[SLICE_PROMPT_STRUCTURE]
    agent_emb = atv[SLICE_AGENT_STATE_EMBEDDING][:32]  # first 32 dims of 768
    blast = atv[SLICE_ACTION_BLAST_RADIUS]
    action_hist = atv[SLICE_ACTION_HISTORY][:32]
    mem_prov = atv[SLICE_MEMORY_PROVENANCE]
    iag = atv[SLICE_INTER_AGENT_GRAPH]

    residency, residency_reasons = _residency_from_signals(
        cache_hit_rate, composite_novelty, progress, context_util,
    )
    speculative, spec_reason = _speculative_eligible(prompt_struct, composite_novelty)

    # Prefetch: if hot/warm, name segments we expect to revisit.
    if residency == "hot":
        prefetch = (
            _segment_ids_from_band(mem_prov, 4, prefix="mem")
            + _segment_ids_from_band(iag, 2, prefix="iag")
        )
        evict: list[str] = []
    elif residency == "warm":
        prefetch = _segment_ids_from_band(mem_prov, 2, prefix="mem")
        evict = []
    else:
        prefetch = []
        # Cold path: name eviction candidates from action history tail.
        evict = _segment_ids_from_band(action_hist, 4, prefix="hist")

    batch_key = _stable_batch_key(agent_emb, blast)

    reasons: list[str] = list(residency_reasons)
    reasons.append(spec_reason)
    reasons.append(
        f"cumulative_tokens={cum_tokens:.0f}, batch_key={batch_key[:8]}…"
    )

    # Confidence: weighted by how much *signal* the cost band carries.
    # If s-10/s-11/s-15 are all 0 (host hasn't filled them), confidence
    # collapses → runtime should fall back to its own heuristic.
    signal_strength = (
        min(1.0, cache_hit_rate + context_util + progress)
        + min(1.0, composite_novelty * 2.0)
    ) / 2.0
    confidence = float(min(1.0, max(0.0, signal_strength)))

    elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000

    return KVCacheAdvice(
        prefetch_segment_ids=prefetch,
        evict_candidates=evict,
        residency_class=residency,
        batch_key=batch_key,
        speculative_decode=speculative,
        confidence=confidence,
        reasons=reasons,
        latency_ms=round(elapsed_ms, 3),
        advisor_hash=_ADVISOR_HASH,
    )
