"""Context-window advisor (v3.7) — ATV → ContextAdvice.

Purpose
-------
LLM agents accumulate long histories. Naively keeping every past turn
in the active context blows the token budget. The context advisor
reads the ATV of *each* historical turn (plus the current one) and
emits per-turn keep / summarize / drop hints so the host can build
the next prompt under a token budget while preserving the most
*relevant* turns.

Different from KV cache advisor
-------------------------------
* KV cache advisor (v3.1) decides **physical memory** (HBM tiers,
  prefetch, evict). It works at the runtime/serving layer.
* Context advisor (v3.7) decides **logical token budget**. It works
  at the prompt-construction layer (host or agent framework).

Both consume the same ATV — different output projections.

Algorithm (deterministic, sub-millisecond)
------------------------------------------
For every historical turn ``t``:

1. Compute a relevance score ∈ [0, 1] vs the current ATV from four
   signals (all already in the 2080-D vector):

   * ``agent_state_embedding`` cosine — semantic distance
   * ``task_progress_score`` (s-15) match — same task phase boost
   * ``composite_novelty`` proximity — similar exploratory state
   * recency weight — newer turns get a base bonus

2. Sort turns by **relevance / token_cost ROI** (worst first to drop).

3. Greedy fit under ``token_budget``:
   * score ≥ 0.70 → ``keep_verbatim``
   * 0.30 ≤ score < 0.70 → ``summarize`` (host LLM compresses)
   * score < 0.30 → ``drop`` (or ``replace_with_atv`` once the
     v4.x ATV-projection adapter is trained)

4. ``expected_token_savings`` = sum of dropped + estimated 70 %
   compression from summarised turns.

Patent linkage
--------------
Claim 48 (proposed) — context window advisory head over ATV history.
Claim 49 — subfield-selective compression via ATV diff (deferred).
Claim 50 — unified-head 5th output (v3.7 stays standalone; v3.8
unifies with M13 unified head).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from aegis.schema import (
    SLICE_AGENT_STATE_EMBEDDING,
    SLICE_COST_EFFICIENCY_METRICS,
    SLICE_NOVELTY_SCORE,
)

KeepDecision = Literal["keep_verbatim", "summarize", "replace_with_atv", "drop"]

_COST_S15_TASK_PROGRESS = 14


@dataclass(frozen=True)
class TurnAdvice:
    """Per-turn decision."""

    turn_id: str
    decision: KeepDecision
    score: float
    token_cost: int


@dataclass(frozen=True)
class ContextAdvice:
    keep_verbatim_turn_ids: list[str] = field(default_factory=list)
    summarize_turn_ids: list[str] = field(default_factory=list)
    replace_with_atv_turn_ids: list[str] = field(default_factory=list)
    drop_turn_ids: list[str] = field(default_factory=list)
    per_turn: list[TurnAdvice] = field(default_factory=list)
    expected_token_savings: int = 0
    total_token_cost_after: int = 0
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    advisor_hash: str = ""


_VERSION = "context_advisor_v1"
_HASH = hashlib.sha3_256(_VERSION.encode()).hexdigest()

# Tunables (frozen part of advisor_hash via _VERSION)
_KEEP_THRESHOLD = 0.70
_SUMMARIZE_THRESHOLD = 0.30
_SUMMARY_COMPRESSION_RATIO = 0.30   # summarised turn ≈ 30 % of original tokens
_RECENCY_HALF_LIFE_TURNS = 8.0


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _relevance(
    *,
    current_state: np.ndarray,
    current_progress: float,
    current_novelty: float,
    turn_state: np.ndarray,
    turn_progress: float,
    turn_novelty: float,
    turns_back: int,
) -> tuple[float, list[str]]:
    """Per-turn relevance ∈ [0, 1] with reason trace."""
    reasons: list[str] = []

    # 1. Semantic distance via agent_state_embedding cosine
    sim = _cosine(current_state, turn_state)
    sim01 = max(0.0, min(1.0, (sim + 1.0) / 2.0))  # cosine [-1,1] → [0,1]

    # 2. Task phase match: closer progress = higher
    progress_match = 1.0 - min(1.0, abs(current_progress - turn_progress))

    # 3. Novelty proximity (0 at extreme difference, 1 at identical)
    novelty_match = 1.0 - min(1.0, abs(current_novelty - turn_novelty))

    # 4. Recency weight: exponential decay with turns_back
    recency = float(np.exp(-turns_back / _RECENCY_HALF_LIFE_TURNS))

    # Weighted sum (weights sum to 1.0)
    score = (
        0.45 * sim01
        + 0.20 * progress_match
        + 0.10 * novelty_match
        + 0.25 * recency
    )
    score = float(min(1.0, max(0.0, score)))
    reasons.append(
        f"sim={sim01:.2f} progress={progress_match:.2f} "
        f"novelty={novelty_match:.2f} recency={recency:.2f} → {score:.2f}"
    )
    return score, reasons


def context_advisor(
    current_atv: np.ndarray,
    history_atvs: list[np.ndarray],
    history_turn_ids: list[str],
    history_token_costs: list[int],
    *,
    token_budget: int,
) -> ContextAdvice:
    """Pure function — sub-millisecond, deterministic.

    Parameters
    ----------
    current_atv:
        2080-D ATV of the current (upcoming) turn.
    history_atvs:
        Past turn ATVs in chronological order (oldest first).
    history_turn_ids:
        Parallel list of stable IDs (used in the output payload so
        host can map decisions back to its turn objects).
    history_token_costs:
        Per-turn token counts. Drives the budget fit.
    token_budget:
        Total tokens allowed for the historical span (excludes the
        current turn's prompt + system; that's the host's concern).

    Returns
    -------
    ContextAdvice
    """
    t0 = time.perf_counter_ns()

    if not (len(history_atvs) == len(history_turn_ids) == len(history_token_costs)):
        raise ValueError(
            "history_atvs / history_turn_ids / history_token_costs must be parallel"
        )

    if current_atv.shape[0] != 2080:
        raise ValueError(f"current_atv must be 2080-D, got {current_atv.shape}")

    cur_state = current_atv[SLICE_AGENT_STATE_EMBEDDING]
    cur_cost = current_atv[SLICE_COST_EFFICIENCY_METRICS]
    cur_progress = float(cur_cost[_COST_S15_TASK_PROGRESS])
    cur_novelty_band = current_atv[SLICE_NOVELTY_SCORE]
    cur_novelty = float(cur_novelty_band[3]) if cur_novelty_band.size >= 4 else 0.0

    n = len(history_atvs)
    per_turn: list[TurnAdvice] = []
    reasons_global: list[str] = []

    if n == 0:
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000
        return ContextAdvice(
            confidence=0.0,
            reasons=["no history"],
            latency_ms=round(elapsed_ms, 3),
            advisor_hash=_HASH,
        )

    # Compute per-turn scores
    scored: list[tuple[int, float]] = []  # (idx, score)
    for i in range(n):
        atv_i = history_atvs[i]
        if atv_i.shape[0] != 2080:
            raise ValueError(f"history_atvs[{i}] must be 2080-D")
        state_i = atv_i[SLICE_AGENT_STATE_EMBEDDING]
        cost_i = atv_i[SLICE_COST_EFFICIENCY_METRICS]
        progress_i = float(cost_i[_COST_S15_TASK_PROGRESS])
        novel_i_band = atv_i[SLICE_NOVELTY_SCORE]
        novel_i = float(novel_i_band[3]) if novel_i_band.size >= 4 else 0.0
        turns_back = n - 1 - i  # most recent has turns_back = 0
        score, _per_reasons = _relevance(
            current_state=cur_state,
            current_progress=cur_progress,
            current_novelty=cur_novelty,
            turn_state=state_i,
            turn_progress=progress_i,
            turn_novelty=novel_i,
            turns_back=turns_back,
        )
        scored.append((i, score))

    # Greedy fit: keep highest-scoring verbatim turns until budget would be exceeded.
    # Then demote next tier to summarize. Anything beyond goes to drop.
    # Rank by (score desc, recency desc) so ties favour newer.
    ranking = sorted(
        scored, key=lambda kv: (kv[1], -(n - 1 - kv[0])), reverse=True,
    )

    decisions: dict[int, KeepDecision] = {i: "drop" for i in range(n)}
    used = 0
    total_original = sum(history_token_costs)
    for idx, score in ranking:
        cost = history_token_costs[idx]
        if score >= _KEEP_THRESHOLD and used + cost <= token_budget:
            decisions[idx] = "keep_verbatim"
            used += cost
        elif score >= _SUMMARIZE_THRESHOLD:
            summary_cost = max(1, int(cost * _SUMMARY_COMPRESSION_RATIO))
            if used + summary_cost <= token_budget:
                decisions[idx] = "summarize"
                used += summary_cost
            else:
                decisions[idx] = "drop"
        else:
            decisions[idx] = "drop"

    # Build per-turn list + bucket aggregations
    keep_ids: list[str] = []
    summarize_ids: list[str] = []
    drop_ids: list[str] = []
    for i in range(n):
        d = decisions[i]
        per_turn.append(TurnAdvice(
            turn_id=history_turn_ids[i],
            decision=d,
            score=next(s for j, s in scored if j == i),
            token_cost=history_token_costs[i],
        ))
        if d == "keep_verbatim":
            keep_ids.append(history_turn_ids[i])
        elif d == "summarize":
            summarize_ids.append(history_turn_ids[i])
        else:
            drop_ids.append(history_turn_ids[i])

    expected_savings = total_original - used
    reasons_global.append(
        f"budget={token_budget}, used={used}, savings={expected_savings} "
        f"({len(keep_ids)} keep, {len(summarize_ids)} summarize, {len(drop_ids)} drop)"
    )

    # Confidence: how much signal does the cost band carry?
    has_signal = (
        (cur_progress > 0)
        + (cur_novelty > 0)
        + (any(history_token_costs))
        + (n >= 2)
    )
    confidence = float(min(1.0, has_signal / 4.0))

    elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000

    return ContextAdvice(
        keep_verbatim_turn_ids=keep_ids,
        summarize_turn_ids=summarize_ids,
        replace_with_atv_turn_ids=[],  # reserved for v4.x adapter
        drop_turn_ids=drop_ids,
        per_turn=per_turn,
        expected_token_savings=expected_savings,
        total_token_cost_after=used,
        confidence=confidence,
        reasons=reasons_global,
        latency_ms=round(elapsed_ms, 3),
        advisor_hash=_HASH,
    )
