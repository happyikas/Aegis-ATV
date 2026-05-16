"""Reward shaping for the autonomy learner (v0.5.12).

v0.5.11 used a binary reward signal: a REQUIRE_APPROVAL record
either was or wasn't followed by a BLOCK in its aid timeline.
That conflated three categories of evidence with very different
information content:

* **CLEAN**: the operator (or downstream firewall) silently let
  the agent continue. *Plentiful, low information.*
* **BLOCK_FOLLOWUP**: the firewall fired a hard BLOCK shortly
  after the REQUIRE_APPROVAL. *Rare, high information.*
* **EXPLICIT_DENY**: the operator actively pressed "deny" or used
  ``aegis autonomy deny <trace_id>`` to negate a past auto-bypass.
  *Rarer still, highest information — direct human signal.*

This module assigns each event a weight so the posterior update
balances them correctly:

* ``CLEAN``           ⇒ ``α += 1``
* ``BLOCK_FOLLOWUP``  ⇒ ``β += 3``
* ``EXPLICIT_DENY``   ⇒ ``β += 10``

Calibration: one EXPLICIT_DENY costs 10 cleans to recover from.
This means a pattern with 100 cleans and 1 explicit deny still has
``α ≈ 101``, ``β ≈ 15`` ⇒ mean ≈ 0.87 (still trustworthy) — but
a pattern with 5 cleans and 1 explicit deny has ``α = 6``, ``β
= 15`` ⇒ mean ≈ 0.29 (firmly distrusted). The asymmetry mirrors
how a real operator updates their belief: a single emphatic "no"
overrides routine "didn't object".

These weights are deliberately *not* tunable at runtime to prevent
the operator from accidentally setting ``weight_deny = 0`` (which
would silently undo the entire negative-signal pathway). Callers
that need different weights for tests can construct posteriors
directly via :func:`make_posterior`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from aegis.context_memory.record import ContextMemoryRecord


class RewardEvent(Enum):
    """The three categories of evidence the learner consumes.

    Encoded as an Enum rather than free strings so that adding a
    fourth category later (e.g. ``RETRY_AFTER_FAILURE``) is an
    obvious typed change rather than a silent string-equality
    drift."""

    CLEAN = "clean"
    BLOCK_FOLLOWUP = "block_followup"
    EXPLICIT_DENY = "explicit_deny"


# ──────────────────────────────────────────────────────────────────
# Default weights
# ──────────────────────────────────────────────────────────────────

# Calibration target: one EXPLICIT_DENY ≈ 10 CLEAN, one
# BLOCK_FOLLOWUP ≈ 3 CLEAN. Tuned so that a posterior built from
# real operator behaviour (Aegis dev fleet, May 2026 burn-in)
# produces stable rankings.
WEIGHT_CLEAN: Final[float] = 1.0
WEIGHT_BLOCK_FOLLOWUP: Final[float] = 3.0
WEIGHT_EXPLICIT_DENY: Final[float] = 10.0


def weight_for(event: RewardEvent) -> float:
    """Return the posterior-update weight for one event."""
    if event is RewardEvent.CLEAN:
        return WEIGHT_CLEAN
    if event is RewardEvent.BLOCK_FOLLOWUP:
        return WEIGHT_BLOCK_FOLLOWUP
    if event is RewardEvent.EXPLICIT_DENY:
        return WEIGHT_EXPLICIT_DENY
    # Unreachable: Enum is closed. Defensive default = treat as
    # CLEAN so the learner errs toward including the signal rather
    # than silently dropping it on a future schema bump.
    return WEIGHT_CLEAN


# ──────────────────────────────────────────────────────────────────
# Event extraction from ContextMemory records
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RewardSignal:
    """One reward-shaped observation derived from a ContextMemory
    record. Carries the source ``trace_id`` so the audit chain
    can reproduce which evidence updated which pattern."""

    event: RewardEvent
    trace_id: str
    ts_ns: int
    tool_name: str
    reason_signature: str
    # The reason this record contributed an EXPLICIT_DENY — empty
    # for other event types. Stored so an operator running
    # ``aegis autonomy explain`` can read the negative-signal
    # chain in plain prose.
    deny_note: str = field(default="")


_DENY_STAMP_KEY: Final[str] = "aegis.autonomy.user_deny"
"""``step_traces`` key set by ``aegis autonomy deny <trace_id>`` to
mark a past record as an explicit user denial. Picked up here at
learn time."""


def classify_record(
    record: ContextMemoryRecord,
    *,
    block_within: Iterable[ContextMemoryRecord] = (),
) -> RewardEvent | None:
    """Classify a single REQUIRE_APPROVAL record into one of the
    three reward events. Returns ``None`` if the record is not a
    REQUIRE_APPROVAL (no signal contribution).

    ``block_within`` is the sliced subsequent timeline (typically
    ≤10 records) within the same aid; we consult it to detect
    BLOCK_FOLLOWUP. Pre-slicing the timeline at the call site
    keeps this function O(window) rather than O(records)."""
    if record.decision != "REQUIRE_APPROVAL":
        return None

    # Explicit user deny stamped via `aegis autonomy deny`. This
    # is the strongest negative signal — overrides any silent
    # clean reading from the timeline.
    if _DENY_STAMP_KEY in (record.step_traces or {}):
        return RewardEvent.EXPLICIT_DENY

    for follow in block_within:
        if follow.decision == "BLOCK":
            return RewardEvent.BLOCK_FOLLOWUP
    return RewardEvent.CLEAN


# ──────────────────────────────────────────────────────────────────
# Public summary structure
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RewardCounts:
    """Per-pattern reward tally produced by the learning pass. The
    Beta posterior is constructed from these via
    :func:`aegis.autonomy.bayesian.make_posterior`."""

    n_clean: float = 0.0
    n_block: float = 0.0
    n_deny: float = 0.0

    def add(
        self,
        event: RewardEvent,
        *,
        weight: float = 1.0,
    ) -> RewardCounts:
        """Return a new ``RewardCounts`` with one event added.

        ``weight`` is the decay multiplier (≤ 1.0) used by
        :mod:`aegis.autonomy.decay`; pass 1.0 for raw counts."""
        if event is RewardEvent.CLEAN:
            return RewardCounts(
                n_clean=self.n_clean + weight,
                n_block=self.n_block,
                n_deny=self.n_deny,
            )
        if event is RewardEvent.BLOCK_FOLLOWUP:
            return RewardCounts(
                n_clean=self.n_clean,
                n_block=self.n_block + weight,
                n_deny=self.n_deny,
            )
        if event is RewardEvent.EXPLICIT_DENY:
            return RewardCounts(
                n_clean=self.n_clean,
                n_block=self.n_block,
                n_deny=self.n_deny + weight,
            )
        return self

    @property
    def n_total(self) -> float:
        """Sum of all event counts, used as the gate for
        ``min_samples`` after Bonferroni adjustment."""
        return self.n_clean + self.n_block + self.n_deny


__all__ = [
    "RewardCounts",
    "RewardEvent",
    "RewardSignal",
    "WEIGHT_BLOCK_FOLLOWUP",
    "WEIGHT_CLEAN",
    "WEIGHT_EXPLICIT_DENY",
    "classify_record",
    "weight_for",
]
