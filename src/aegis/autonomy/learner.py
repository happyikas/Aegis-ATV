"""Autonomy learner — burn-in trust pattern miner (v0.5.11).

Closes the user-experience gap: every REQUIRE_APPROVAL today
interrupts the autonomous agent and asks the operator for an
allow/deny click. For a frequent, routine pattern (e.g. "loop
detector fired on Bash" — operator always ends up letting it
proceed after pausing), this becomes pure friction.

This module observes the burn-in window of historical decisions
and learns which REQUIRE_APPROVAL patterns the operator has
seen+handled often enough to trust. At runtime, when a matching
pattern fires:

* The firewall would normally emit REQUIRE_APPROVAL (human in
  the loop).
* The autonomy bypass downgrades it to ALLOW, **but stamps the
  ATV record so the auto-approval is permanently traceable**.
* `aegis doctor` and `aegis autonomy outliers` can grep these
  stamps and surface any auto-approval that turned out poorly —
  closing the audit / postmortem loop.

Trust criteria (any can be tuned via env / kwarg):

1. **Sample count** — pattern observed ≥ ``min_samples`` times in
   the burn-in window. Default 5. Single-incident patterns are
   never auto-bypassed.

2. **Clean follow-up rate** — among records carrying the pattern,
   the fraction that were NOT followed by a BLOCK from the same
   trace within the window. Default ≥ 0.95. A pattern whose
   downstream chain repeatedly ended in BLOCK is dropped — that's
   the "outlier" signal the operator wants flagged.

3. **No destructive escalation** — patterns whose reason text
   contains 'rule:dangerous' / 'dangerous pattern: ' / 'sensitive
   path' are NEVER trusted even with high sample count. The
   operator-friction tradeoff is acceptable for these classes;
   data-loss recovery is not.

Behavior is opt-in via ``AEGIS_AUTONOMY_ENABLED=1`` (env). The
firewall step331_autonomy will be a no-op when the env flag is
not set, so existing deployments see byte-identical behavior.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from aegis.context_memory.record import ContextMemoryRecord

# ──────────────────────────────────────────────────────────────────
# Configuration constants
# ──────────────────────────────────────────────────────────────────

DEFAULT_MIN_SAMPLES: Final[int] = 5
DEFAULT_MIN_CLEAN_RATE: Final[float] = 0.95

# Reason prefixes / substrings that NEVER qualify for auto-bypass.
# Even when an operator has seen + cleared these many times during
# burn-in, the patent + safety contract require keeping the human
# in the loop for these. The cost of one extra approval click is
# tiny vs the cost of an auto-bypassed destructive-action.
_NEVER_TRUST_SUBSTRINGS: Final[tuple[str, ...]] = (
    "dangerous pattern",
    "rule:dangerous",
    "rule:git_destructive",     # git force-push, rebase main, etc.
    "rule:cloud_destructive",   # kubectl delete, terraform destroy
    "sensitive path",
    "cumulative_dollars",       # budget gate — operator must see
)


def autonomy_enabled() -> bool:
    """Return True iff the autonomy bypass is engaged at runtime.

    Off by default (v0.5.11 contract). Operators opt in via
    ``AEGIS_AUTONOMY_ENABLED=1``. Tests + replay never hit the
    bypass path unless this flag is set."""
    raw = os.environ.get("AEGIS_AUTONOMY_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ──────────────────────────────────────────────────────────────────
# Data shapes
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TrustedPattern:
    """A REQUIRE_APPROVAL pattern that's appeared often enough +
    cleanly enough to be auto-bypassed.

    ``reason_signature`` is a canonical form of the firewall's
    REQUIRE_APPROVAL reason — see :func:`reason_signature`. Two
    distinct reasons that share the same root cause (e.g. all
    "same X call repeated N times" loop reasons regardless of N)
    collapse to one signature so the learner can build statistics."""

    tool_name: str
    reason_signature: str
    n_seen: int
    n_followed_by_block: int
    clean_rate: float
    trust_score: float            # 0..1 — derived metric
    last_seen_ns: int
    sample_trace_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> tuple[str, str]:
        """Stable lookup key for the trust table."""
        return (self.tool_name, self.reason_signature)


@dataclass(frozen=True)
class AutonomyVerdict:
    """Outcome of consulting the trust table for one
    REQUIRE_APPROVAL event."""

    auto_approve: bool
    matched_pattern: TrustedPattern | None
    confidence: float             # 0..1; matched_pattern.trust_score
                                  # when matched, 0.0 otherwise
    reason: str                   # operator-facing explanation
    outlier_signals: tuple[str, ...] = field(default_factory=tuple)


# ──────────────────────────────────────────────────────────────────
# Reason signature canonicalisation
# ──────────────────────────────────────────────────────────────────

_LOOP_RE: Final[re.Pattern[str]] = re.compile(
    r"^same (\w+) call repeated (\d+) times this session"
)
_DOLLAR_RE: Final[re.Pattern[str]] = re.compile(
    r"^cumulative_dollars\s+[\d.]+\s*>\s*budget"
)


def reason_signature(reason: str) -> str:
    """Canonical form of a firewall REQUIRE_APPROVAL reason.

    Different concrete reasons that share a root cause map to the
    same signature so the learner can build N-sample statistics.

    Examples:
      "same Bash call repeated 3 times this session (threshold=3)"
        → "loop:Bash"
      "same Bash call repeated 5 times this session (threshold=3)"
        → "loop:Bash"
      "cumulative_dollars 56549.4134 > budget 1.0000"
        → "budget"
      "rule:prompt_injection"
        → "rule:prompt_injection"
      "cost-divergence escalation: token_to_flops = 1.000 > ..."
        → "cost-divergence"
    """
    if not reason:
        return "(empty)"

    m = _LOOP_RE.match(reason)
    if m:
        return f"loop:{m.group(1)}"

    if _DOLLAR_RE.match(reason):
        return "budget"

    if reason.startswith("cost-divergence"):
        return "cost-divergence"

    if reason.startswith("rule:"):
        # rule:foo → rule:foo (keep specific rule names)
        return reason.split()[0]

    if "sensitive path" in reason:
        return "sensitive_path"

    if "dangerous pattern" in reason:
        return "dangerous_pattern"

    # Generic fallback: first 3 tokens. Keeps cardinality
    # bounded so the trust table stays small.
    return " ".join(reason.split()[:3])


def _is_never_trust(reason: str) -> bool:
    """Return True if this reason string contains any
    never-trust substring — these are never auto-bypassed
    regardless of burn-in observation count."""
    return any(s in reason for s in _NEVER_TRUST_SUBSTRINGS)


# ──────────────────────────────────────────────────────────────────
# Learner — observe burn-in window and build trust table
# ──────────────────────────────────────────────────────────────────


@dataclass
class _PatternBucket:
    """Mutable accumulator during the learning pass."""
    tool: str
    signature: str
    n_seen: int = 0
    n_followed_by_block: int = 0
    last_seen_ns: int = 0
    sample_traces: list[str] = field(default_factory=list)


def _detect_block_followup(
    record: ContextMemoryRecord,
    later_records_by_aid: dict[str, list[ContextMemoryRecord]],
) -> bool:
    """Heuristic: was a REQUIRE_APPROVAL record followed (within the
    same aid/session) by a BLOCK in the next ~10 records?

    The 10-record window is empirical — most "approve-but-bad"
    flows show their failure within a handful of subsequent tool
    calls. Wider windows pull in unrelated noise.
    """
    later = later_records_by_aid.get(record.aid, [])
    # Find this record's position in its aid's timeline.
    idx = next(
        (i for i, r in enumerate(later) if r.trace_id == record.trace_id),
        None,
    )
    if idx is None:
        return False
    # Scan the next up-to-10 records for BLOCK.
    return any(
        r.decision == "BLOCK"
        for r in later[idx + 1 : idx + 11]
    )


def learn_trusted_patterns(
    records: Iterable[ContextMemoryRecord],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_clean_rate: float = DEFAULT_MIN_CLEAN_RATE,
) -> dict[tuple[str, str], TrustedPattern]:
    """Walk the burn-in window of records and produce the trust
    table.

    Returns a dict keyed by (tool_name, reason_signature). Each
    value is a :class:`TrustedPattern` summarising the
    observation count + clean-followup rate + trust score.

    A pattern qualifies for inclusion when ALL of:

      * count of REQUIRE_APPROVAL records carrying this (tool,
        signature) ≥ ``min_samples``;
      * clean-followup rate ≥ ``min_clean_rate``;
      * none of its representative reasons contains a never-trust
        substring (see :data:`_NEVER_TRUST_SUBSTRINGS`).

    The returned table is consumable by
    :func:`evaluate_autonomy_request` to decide whether to
    bypass a live REQUIRE_APPROVAL.
    """
    rec_list = list(records)

    # Index records by aid for the block-followup heuristic. We
    # only need the records sorted by ts within each aid.
    by_aid: dict[str, list[ContextMemoryRecord]] = defaultdict(list)
    for r in rec_list:
        by_aid[r.aid].append(r)
    for aid in by_aid:
        by_aid[aid].sort(key=lambda r: r.ts_ns)

    buckets: dict[tuple[str, str], _PatternBucket] = {}
    never_trust_keys: set[tuple[str, str]] = set()

    for r in rec_list:
        if r.decision != "REQUIRE_APPROVAL":
            continue
        sig = reason_signature(r.reason or "")
        key = (r.tool_name, sig)
        # Sticky: once a record under this key contained a never-trust
        # substring, drop the key forever.
        if _is_never_trust(r.reason or ""):
            never_trust_keys.add(key)
            continue
        b = buckets.get(key)
        if b is None:
            b = _PatternBucket(tool=r.tool_name, signature=sig)
            buckets[key] = b
        b.n_seen += 1
        b.last_seen_ns = max(b.last_seen_ns, r.ts_ns)
        if len(b.sample_traces) < 3:
            b.sample_traces.append(r.trace_id)
        if _detect_block_followup(r, by_aid):
            b.n_followed_by_block += 1

    # Build the public dict, applying the threshold filters.
    out: dict[tuple[str, str], TrustedPattern] = {}
    for key, b in buckets.items():
        if key in never_trust_keys:
            continue
        if b.n_seen < min_samples:
            continue
        clean_rate = 1.0 - (b.n_followed_by_block / b.n_seen)
        if clean_rate < min_clean_rate:
            continue
        # Trust score = clean_rate weighted by sample size — more
        # samples = more confident in the clean rate.
        sample_weight = min(1.0, b.n_seen / 20.0)
        trust = clean_rate * (0.6 + 0.4 * sample_weight)
        out[key] = TrustedPattern(
            tool_name=b.tool,
            reason_signature=b.signature,
            n_seen=b.n_seen,
            n_followed_by_block=b.n_followed_by_block,
            clean_rate=clean_rate,
            trust_score=trust,
            last_seen_ns=b.last_seen_ns,
            sample_trace_ids=tuple(b.sample_traces),
        )
    return out


# ──────────────────────────────────────────────────────────────────
# Runtime evaluator — single-decision autonomy verdict
# ──────────────────────────────────────────────────────────────────

MIN_TRUST_FOR_BYPASS: Final[float] = 0.85


def evaluate_autonomy_request(
    *,
    tool_name: str,
    reason: str,
    trust_table: dict[tuple[str, str], TrustedPattern],
    min_trust: float = MIN_TRUST_FOR_BYPASS,
) -> AutonomyVerdict:
    """Given a live REQUIRE_APPROVAL signal, decide whether to
    auto-bypass it based on the learned trust table.

    Returns an :class:`AutonomyVerdict`. ``auto_approve=True``
    means the human prompt should be skipped (firewall verdict
    downgraded from REQUIRE_APPROVAL to ALLOW). ``auto_approve=
    False`` means keep the human in the loop.

    Never returns ``auto_approve=True`` for never-trust reasons
    even when ``AEGIS_AUTONOMY_ENABLED=1`` — the constant filter
    is enforced here, not just at learning time, so an adversarial
    trust table can't sneak through.
    """
    # Sentinel: never trust dangerous categories regardless of
    # what the trust table says.
    if _is_never_trust(reason or ""):
        return AutonomyVerdict(
            auto_approve=False,
            matched_pattern=None,
            confidence=0.0,
            reason="never-trust category — human in the loop preserved",
            outlier_signals=("never_trust_filter",),
        )

    sig = reason_signature(reason or "")
    key = (tool_name, sig)
    pattern = trust_table.get(key)
    if pattern is None:
        return AutonomyVerdict(
            auto_approve=False,
            matched_pattern=None,
            confidence=0.0,
            reason=(
                f"no trust entry for tool={tool_name} "
                f"signature={sig!r} — first occurrence or below "
                "burn-in threshold"
            ),
        )

    if pattern.trust_score < min_trust:
        return AutonomyVerdict(
            auto_approve=False,
            matched_pattern=pattern,
            confidence=pattern.trust_score,
            reason=(
                f"trust score {pattern.trust_score:.2f} below "
                f"min_trust {min_trust:.2f} — keeping human in loop"
            ),
            outlier_signals=("low_trust_score",),
        )

    return AutonomyVerdict(
        auto_approve=True,
        matched_pattern=pattern,
        confidence=pattern.trust_score,
        reason=(
            f"trusted pattern (seen {pattern.n_seen}× in burn-in, "
            f"clean rate {pattern.clean_rate:.0%}, "
            f"trust {pattern.trust_score:.2f})"
        ),
    )


def render_trust_table(
    table: dict[tuple[str, str], TrustedPattern],
) -> str:
    """Plain-text rendering for the `aegis autonomy show` CLI."""
    lines = [
        f"Autonomy trust table — {len(table)} pattern(s) learned",
        "",
    ]
    if not table:
        lines.append(
            "  (empty — run `aegis autonomy learn --since 30d`)"
        )
        return "\n".join(lines)

    # Sort by descending trust score so highest-confidence patterns
    # appear first.
    sorted_patterns = sorted(
        table.values(), key=lambda p: -p.trust_score,
    )
    lines.append(
        f"  {'tool':<14} {'signature':<24} {'seen':>5} "
        f"{'clean':>6} {'trust':>6}"
    )
    lines.append("  " + "-" * 64)
    for p in sorted_patterns:
        lines.append(
            f"  {p.tool_name:<14} {p.reason_signature:<24} "
            f"{p.n_seen:>5} {p.clean_rate:>6.0%} "
            f"{p.trust_score:>6.2f}"
        )
    return "\n".join(lines)


__all__ = [
    "AutonomyVerdict",
    "DEFAULT_MIN_CLEAN_RATE",
    "DEFAULT_MIN_SAMPLES",
    "MIN_TRUST_FOR_BYPASS",
    "TrustedPattern",
    "autonomy_enabled",
    "evaluate_autonomy_request",
    "learn_trusted_patterns",
    "reason_signature",
    "render_trust_table",
]
