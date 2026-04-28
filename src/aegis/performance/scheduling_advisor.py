"""Scheduling advisor (v3.4) — pure function ATV → SchedulingAdvice.

Reads ATV signals to advise the runtime scheduler on:

* **priority_class** — interactive | batch | low
  high human_oversight + active operator → interactive
  no operator + low blast → batch
* **preempt_safe** — can this request be evicted from a batch slot
  for a higher-priority request without violating semantics?
  yes when the agent_state is read-only + no in-flight side-effect.
* **max_concurrent_in_cohort** — runtime can run N peers from the same
  batch_key concurrently. Rises with low novelty + low blast.
* **deadline_ms** — soft latency budget the runtime should target.
  derived from human_oversight.human_response_p95.

Patent linkage
--------------
Same M13 attribution structure (frozen weights, sub-millisecond,
deterministic) — different output head. Pure function of ATV.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from aegis.schema import (
    SLICE_ACTION_BLAST_RADIUS,
    SLICE_AID_ATS_SCALARS,
    SLICE_HUMAN_OVERSIGHT_STATE,
    SLICE_NOVELTY_SCORE,
    SLICE_TOOL_ARG_INSPECTION,
    ATVInput,
)

PriorityClass = Literal["interactive", "batch", "low"]


@dataclass(frozen=True)
class SchedulingAdvice:
    priority_class: PriorityClass = "batch"
    preempt_safe: bool = True
    max_concurrent_in_cohort: int = 4
    deadline_ms: int = 5000
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    advisor_hash: str = ""


_VERSION = "scheduling_advisor_v1"
_HASH = hashlib.sha3_256(_VERSION.encode()).hexdigest()


def scheduling_advisor(
    atv: np.ndarray,
    inp: ATVInput | None = None,
) -> SchedulingAdvice:
    """ATV → SchedulingAdvice. Pure function, sub-millisecond."""
    t0 = time.perf_counter_ns()

    blast = atv[SLICE_ACTION_BLAST_RADIUS]
    blast_norm = float(blast[0]) if blast.size > 0 else 0.0  # idx 0 = blast_radius_norm

    oversight = atv[SLICE_HUMAN_OVERSIGHT_STATE]  # 8-D
    operator_present = float(oversight[0]) if oversight.size >= 1 else 0.0
    response_p95 = float(oversight[7]) if oversight.size >= 8 else 0.0

    novelty_band = atv[SLICE_NOVELTY_SCORE]
    composite_novelty = float(novelty_band[3]) if novelty_band.size >= 4 else 0.0

    arg_band = atv[SLICE_TOOL_ARG_INSPECTION]
    has_destructive_verb = float(arg_band[0]) if arg_band.size > 0 else 0.0  # idx 0
    has_filesystem_write = float(arg_band[6]) if arg_band.size >= 7 else 0.0  # idx 6

    aid_band = atv[SLICE_AID_ATS_SCALARS]
    is_t3 = float(aid_band[4]) if aid_band.size >= 5 else 0.0  # idx 4

    reasons: list[str] = []

    # Priority
    if operator_present > 0.5:
        priority: PriorityClass = "interactive"
        reasons.append("interactive: operator_present > 0.5")
    elif blast_norm < 0.30 and composite_novelty < 0.30:
        priority = "batch"
        reasons.append(
            f"batch: blast={blast_norm:.2f} < 0.30, novelty={composite_novelty:.2f} < 0.30"
        )
    elif blast_norm > 0.70:
        priority = "interactive"
        reasons.append(f"interactive: high-blast ({blast_norm:.2f}) needs prompt observation")
    else:
        priority = "low"
        reasons.append("low: no clear priority signal")

    # Preempt safety: read-only + no destructive verb + no filesystem write
    preempt_safe = (
        has_destructive_verb < 0.5
        and has_filesystem_write < 0.5
        and blast_norm < 0.30
    )
    if preempt_safe:
        reasons.append("preempt_safe=True: read-only profile")
    else:
        reasons.append("preempt_safe=False: side-effect risk")

    # Cohort concurrency: low novelty + low blast → fanout safe
    if composite_novelty < 0.20 and blast_norm < 0.30:
        max_concurrent = 16
    elif composite_novelty < 0.40:
        max_concurrent = 8
    else:
        max_concurrent = 2  # high novelty: serialise
    reasons.append(f"max_concurrent={max_concurrent}")

    # Deadline: scaled from human_response_p95 (ms units in [0,1] norm).
    if priority == "interactive":
        deadline = max(500, int(response_p95 * 5000) or 2000)
    elif priority == "batch":
        deadline = 30000
    else:
        deadline = 60000
    reasons.append(f"deadline_ms={deadline}")

    # Confidence: how much signal do we have about the workload?
    has_signal = (
        (operator_present > 0)
        + (composite_novelty > 0)
        + (blast_norm > 0)
        + (is_t3 > 0)
    )
    confidence = float(min(1.0, has_signal / 4.0))

    elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000

    return SchedulingAdvice(
        priority_class=priority,
        preempt_safe=preempt_safe,
        max_concurrent_in_cohort=max_concurrent,
        deadline_ms=deadline,
        confidence=confidence,
        reasons=reasons,
        latency_ms=round(elapsed_ms, 3),
        advisor_hash=_HASH,
    )
