"""Knowledge-layer builder — derive wiki entries from raw events.

Given a list of :class:`ContextMemoryRecord`s, produce one
:class:`KnowledgeEntry` per agent, tool, and pattern signature
that appears in the window. The output is consumable by the
sLLM advisor without re-reading the raw store.

Per-entry-kind builders:

* **AGENT** (one per ``aid``) — overall activity, stability,
  cost profile. References the agent's top tools + patterns.
* **TOOL** (one per ``tool_name``) — usage volume, latency,
  block reasons. References the patterns that fire on it.
* **PATTERN** (one per ``(tool_name, reason_signature)``) —
  conditions, outcomes, agents that hit it most.

The builder is **pure** — given the same record list it produces
identical entries (modulo the embedded build timestamp). Re-build
is the operator-explicit way to refresh the wiki; we don't try
to update entries incrementally, because the rebuild is cheap
(O(records)) and atomic.

The narrative prose in each ``Section.body`` is deliberately
short and factual. The sLLM advisor wraps it in its own
reasoning prose; we just supply the raw evidence.

Confidence rule (see also :class:`KnowledgeEntry.confidence`):
   confidence = min(1.0, n_observations / 50.0)
…so a 50+ observation entry hits full confidence; smaller
samples linearly drop. This matches the autonomy learner's
"5 = bare minimum, 50 = solid" empirical thresholds.
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from aegis.autonomy.learner import reason_signature
from aegis.context_memory.record import ContextMemoryRecord
from aegis.knowledge.schema import (
    EntryKind,
    InfoBox,
    KnowledgeEntry,
    Section,
    make_entry_id,
)

# ──────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────

CONFIDENCE_FULL_AT: Final[int] = 50
"""Number of observations at which an entry reaches confidence 1.0.
Below this the confidence is linear in ``n / CONFIDENCE_FULL_AT``."""

TOP_K_TOOLS_PER_AGENT: Final[int] = 5
"""Number of top tools to surface in an agent's entry."""

TOP_K_PATTERNS_PER_TOOL: Final[int] = 5
"""Number of top patterns to surface in a tool's entry."""

TOP_K_AGENTS_PER_PATTERN: Final[int] = 3
"""Number of top agents to surface in a pattern's entry."""

# v0.5.20 — event-level entry tuning
SESSION_GAP_SECONDS: Final[int] = 30 * 60
"""Inactivity gap that splits the timeline into separate SESSION
entries. 30 minutes is the conventional "lunch break" boundary —
shorter splits over-fragment the wiki; longer joins make sessions
too long to summarise meaningfully."""

SESSION_MIN_CALLS: Final[int] = 5
"""Sessions shorter than this are skipped unless they contain a
BLOCK (those are kept regardless, since one-call BLOCK sessions
are themselves interesting)."""

INCIDENT_BEFORE: Final[int] = 3
"""Calls preceding a BLOCK to include as setup context."""

INCIDENT_AFTER: Final[int] = 2
"""Calls following a BLOCK to include as recovery context."""

WORKFLOW_MIN_OCCURRENCES: Final[int] = 3
"""Tool-sequence (bigram) frequency at which we promote it to a
WORKFLOW entry. Fewer than 3 occurrences is too noisy to call a
"workflow"; the broader pattern miners (AGENT / PATTERN) cover
those."""

WORKFLOW_SEQUENCE_LEN: Final[int] = 2
"""Bigram length. Trigrams produce more-specific workflows but
explode combinatorially on small datasets; we start with bigrams."""


def _confidence_from_n(n: int) -> float:
    """Linear-saturating confidence schedule."""
    if n <= 0:
        return 0.0
    return min(1.0, n / float(CONFIDENCE_FULL_AT))


# ──────────────────────────────────────────────────────────────────
# Per-agent accumulator
# ──────────────────────────────────────────────────────────────────


@dataclass
class _AgentBucket:
    aid: str
    n_total: int = 0
    n_allow: int = 0
    n_approval: int = 0
    n_block: int = 0
    total_cost_usd: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    sum_latency_ms: float = 0.0
    tool_counts: Counter[str] = field(default_factory=Counter)
    pattern_counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    block_reason_counts: Counter[str] = field(default_factory=Counter)
    ts_first_ns: int = 0
    ts_last_ns: int = 0


@dataclass
class _ToolBucket:
    tool: str
    n_total: int = 0
    n_allow: int = 0
    n_approval: int = 0
    n_block: int = 0
    sum_cost_usd: float = 0.0
    sum_latency_ms: float = 0.0
    pattern_counts: Counter[str] = field(default_factory=Counter)
    block_reason_counts: Counter[str] = field(default_factory=Counter)
    aid_counts: Counter[str] = field(default_factory=Counter)
    ts_first_ns: int = 0
    ts_last_ns: int = 0


@dataclass
class _PatternBucket:
    tool: str
    signature: str
    n_fires: int = 0
    n_block_followup: int = 0
    n_bypass_applied: int = 0
    aid_counts: Counter[str] = field(default_factory=Counter)
    sample_reasons: list[str] = field(default_factory=list)
    sum_cost_usd: float = 0.0
    ts_first_ns: int = 0
    ts_last_ns: int = 0


# ──────────────────────────────────────────────────────────────────
# Builder entry point
# ──────────────────────────────────────────────────────────────────


def build_knowledge(
    records: Iterable[ContextMemoryRecord],
    *,
    bypass_lookahead: int = 10,
) -> list[KnowledgeEntry]:
    """Derive the full set of knowledge entries from raw records.

    Returns the entries in a deterministic order: agents first
    (by aid), then tools (by name), then patterns (by salience).
    The order matters for ``aegis knowledge list`` and for
    git-diff stability if the operator commits the wiki.

    ``bypass_lookahead`` is the same window as the autonomy
    outlier walker — used here to detect "this auto-approval was
    followed by a BLOCK" for the PATTERN entries' outcome
    statistics."""
    rec_list = list(records)
    if not rec_list:
        return []

    agent_buckets: dict[str, _AgentBucket] = {}
    tool_buckets: dict[str, _ToolBucket] = {}
    pattern_buckets: dict[tuple[str, str], _PatternBucket] = {}

    # Index records by aid for the bypass-followup heuristic
    # used in PATTERN entries (same idiom as the autonomy learner).
    by_aid: dict[str, list[ContextMemoryRecord]] = defaultdict(list)
    for r in rec_list:
        by_aid[r.aid].append(r)
    for aid in by_aid:
        by_aid[aid].sort(key=lambda r: r.ts_ns)

    for r in rec_list:
        _accumulate_agent(agent_buckets, r)
        _accumulate_tool(tool_buckets, r)
        if r.decision == "REQUIRE_APPROVAL":
            _accumulate_pattern(
                pattern_buckets, r, by_aid,
                bypass_lookahead=bypass_lookahead,
            )

    entries: list[KnowledgeEntry] = []
    for aid in sorted(agent_buckets.keys()):
        entries.append(_render_agent_entry(agent_buckets[aid]))
    for tool in sorted(tool_buckets.keys()):
        entries.append(_render_tool_entry(tool_buckets[tool]))
    for key in sorted(
        pattern_buckets.keys(),
        key=lambda k: -pattern_buckets[k].n_fires,
    ):
        entries.append(_render_pattern_entry(pattern_buckets[key]))

    # v0.5.20 — event-level entries (SESSION / INCIDENT / WORKFLOW)
    # are derived per-aid from the same by_aid index already built
    # for the bypass-followup heuristic. Each is appended in a
    # deterministic order so the wiki is git-diff stable.
    for aid in sorted(by_aid.keys()):
        timeline = by_aid[aid]

        # SESSION — gap-segmented bursts.
        for session in _segment_sessions(timeline):
            entry = _render_session_entry(aid, session)
            if entry is not None:
                entries.append(entry)

        # INCIDENT — one per BLOCK on this timeline.
        for r in timeline:
            if r.decision != "BLOCK":
                continue
            entry = _render_incident_entry(aid, r, timeline)
            if entry is not None:
                entries.append(entry)

        # WORKFLOW — recurring tool n-grams.
        entries.extend(_mine_workflows(aid, timeline))

    return entries


# ──────────────────────────────────────────────────────────────────
# Accumulators
# ──────────────────────────────────────────────────────────────────


def _accumulate_agent(
    buckets: dict[str, _AgentBucket],
    r: ContextMemoryRecord,
) -> None:
    aid = r.aid or "(unknown)"
    b = buckets.get(aid)
    if b is None:
        b = _AgentBucket(aid=aid, ts_first_ns=r.ts_ns)
        buckets[aid] = b
    b.n_total += 1
    if r.decision == "ALLOW":
        b.n_allow += 1
    elif r.decision == "REQUIRE_APPROVAL":
        b.n_approval += 1
    elif r.decision == "BLOCK":
        b.n_block += 1
        if r.reason:
            b.block_reason_counts[r.reason[:80]] += 1
    b.total_cost_usd += float(r.cost_usd or 0.0)
    b.total_tokens_in += int(r.tokens_in or 0)
    b.total_tokens_out += int(r.tokens_out or 0)
    b.sum_latency_ms += float(r.latency_ms or 0.0)
    if r.tool_name:
        b.tool_counts[r.tool_name] += 1
    if r.decision == "REQUIRE_APPROVAL":
        sig = reason_signature(r.reason or "")
        b.pattern_counts[(r.tool_name, sig)] += 1
    if r.ts_ns:
        if b.ts_first_ns == 0 or r.ts_ns < b.ts_first_ns:
            b.ts_first_ns = r.ts_ns
        if r.ts_ns > b.ts_last_ns:
            b.ts_last_ns = r.ts_ns


def _accumulate_tool(
    buckets: dict[str, _ToolBucket],
    r: ContextMemoryRecord,
) -> None:
    tool = r.tool_name or "(unknown)"
    b = buckets.get(tool)
    if b is None:
        b = _ToolBucket(tool=tool, ts_first_ns=r.ts_ns)
        buckets[tool] = b
    b.n_total += 1
    if r.decision == "ALLOW":
        b.n_allow += 1
    elif r.decision == "REQUIRE_APPROVAL":
        b.n_approval += 1
        sig = reason_signature(r.reason or "")
        b.pattern_counts[sig] += 1
    elif r.decision == "BLOCK":
        b.n_block += 1
        if r.reason:
            b.block_reason_counts[r.reason[:80]] += 1
    b.sum_cost_usd += float(r.cost_usd or 0.0)
    b.sum_latency_ms += float(r.latency_ms or 0.0)
    if r.aid:
        b.aid_counts[r.aid] += 1
    if r.ts_ns:
        if b.ts_first_ns == 0 or r.ts_ns < b.ts_first_ns:
            b.ts_first_ns = r.ts_ns
        if r.ts_ns > b.ts_last_ns:
            b.ts_last_ns = r.ts_ns


def _accumulate_pattern(
    buckets: dict[tuple[str, str], _PatternBucket],
    r: ContextMemoryRecord,
    by_aid: dict[str, list[ContextMemoryRecord]],
    *,
    bypass_lookahead: int,
) -> None:
    sig = reason_signature(r.reason or "")
    key = (r.tool_name, sig)
    b = buckets.get(key)
    if b is None:
        b = _PatternBucket(tool=r.tool_name, signature=sig, ts_first_ns=r.ts_ns)
        buckets[key] = b
    b.n_fires += 1
    if r.aid:
        b.aid_counts[r.aid] += 1
    if r.cost_usd:
        b.sum_cost_usd += float(r.cost_usd)
    if r.reason and len(b.sample_reasons) < 3:
        b.sample_reasons.append(r.reason[:100])

    # Bypass detection: does this record carry the step331.run
    # stamp? If so, the autonomy layer auto-approved it. Useful
    # for the PATTERN entry's outcomes section.
    if "aegis.autonomy.step331.run" in (r.step_traces or {}):
        b.n_bypass_applied += 1

    # BLOCK-followup detection.
    timeline = by_aid.get(r.aid, [])
    idx = next(
        (i for i, x in enumerate(timeline) if x.trace_id == r.trace_id),
        None,
    )
    if idx is not None:
        for follow in timeline[idx + 1 : idx + 1 + bypass_lookahead]:
            if follow.decision == "BLOCK":
                b.n_block_followup += 1
                break

    if r.ts_ns:
        if b.ts_first_ns == 0 or r.ts_ns < b.ts_first_ns:
            b.ts_first_ns = r.ts_ns
        if r.ts_ns > b.ts_last_ns:
            b.ts_last_ns = r.ts_ns


# ──────────────────────────────────────────────────────────────────
# Entry rendering helpers
# ──────────────────────────────────────────────────────────────────


def _fmt_ns(ts_ns: int) -> str:
    """Render a nanosecond timestamp as YYYY-MM-DD HH:MM UTC."""
    if ts_ns <= 0:
        return "(unknown)"
    try:
        return _dt.datetime.fromtimestamp(
            ts_ns / 1e9, tz=_dt.UTC,
        ).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError):
        return "(unparseable)"


def _pct(num: int, denom: int) -> str:
    """Percentage string with two decimals, or '0.00%' on zero
    denominator. Helps keep section bodies clean."""
    if denom <= 0:
        return "0.00%"
    return f"{(num / denom) * 100.0:.2f}%"


# ──────────────────────────────────────────────────────────────────
# AGENT entry rendering
# ──────────────────────────────────────────────────────────────────


def _render_agent_entry(b: _AgentBucket) -> KnowledgeEntry:
    """One wiki article per agent (aid). Three profile sections:
    activity, stability, cost. Cross-refs top tools + patterns."""
    top_tools = b.tool_counts.most_common(TOP_K_TOOLS_PER_AGENT)
    top_patterns = b.pattern_counts.most_common(TOP_K_TOOLS_PER_AGENT)
    top_blocks = b.block_reason_counts.most_common(3)

    avg_latency = (b.sum_latency_ms / b.n_total) if b.n_total > 0 else 0.0
    avg_cost = (b.total_cost_usd / b.n_total) if b.n_total > 0 else 0.0
    block_rate = _pct(b.n_block, b.n_total)
    approval_rate = _pct(b.n_approval, b.n_total)

    summary = (
        f"Coding agent active between {_fmt_ns(b.ts_first_ns)} and "
        f"{_fmt_ns(b.ts_last_ns)}: {b.n_total:,} tool calls, "
        f"{block_rate} BLOCK rate, ${b.total_cost_usd:.4f} total cost."
    )

    infobox = InfoBox(fields={
        "first_seen": _fmt_ns(b.ts_first_ns),
        "last_seen": _fmt_ns(b.ts_last_ns),
        "n_calls": b.n_total,
        "n_allow": b.n_allow,
        "n_require_approval": b.n_approval,
        "n_block": b.n_block,
        "total_cost_usd": round(b.total_cost_usd, 4),
        "avg_cost_per_call_usd": round(avg_cost, 6),
        "avg_latency_ms": round(avg_latency, 2),
        "tokens_in": b.total_tokens_in,
        "tokens_out": b.total_tokens_out,
    })

    sections: list[Section] = []
    if top_tools:
        sections.append(Section(
            heading="Activity profile",
            body=(
                "**Top tools** (by frequency):\n\n"
                + "\n".join(
                    f"- `{tool}`: {n:,} calls ({_pct(n, b.n_total)})"
                    for tool, n in top_tools
                )
            ),
        ))

    stability_body_parts: list[str] = [
        f"- BLOCK rate: **{block_rate}** ({b.n_block:,} events)",
        f"- REQUIRE_APPROVAL rate: {approval_rate} ({b.n_approval:,} events)",
    ]
    if top_blocks:
        stability_body_parts.append("")
        stability_body_parts.append("**Top BLOCK reasons**:")
        for reason, n in top_blocks:
            stability_body_parts.append(f"- {reason!r}: {n}")
    sections.append(Section(
        heading="Stability profile",
        body="\n".join(stability_body_parts),
    ))

    cost_body = (
        f"- 30-day total: **${b.total_cost_usd:.4f}**\n"
        f"- Average per call: ${avg_cost:.6f}\n"
        f"- Tokens in: {b.total_tokens_in:,}\n"
        f"- Tokens out: {b.total_tokens_out:,}"
    )
    sections.append(Section(heading="Cost profile", body=cost_body))

    if top_patterns:
        pat_lines = [
            f"- pattern/{sig} on `{tool}`: {n} fires"
            for (tool, sig), n in top_patterns
        ]
        sections.append(Section(
            heading="Patterns observed",
            body=(
                "REQUIRE_APPROVAL patterns that fired for this agent:\n\n"
                + "\n".join(pat_lines)
            ),
        ))

    related: list[str] = []
    related.extend(make_entry_id(EntryKind.TOOL, t) for t, _ in top_tools)
    related.extend(
        make_entry_id(EntryKind.PATTERN, f"{tool}:{sig}")
        for (tool, sig), _ in top_patterns
    )

    tags: list[str] = []
    if b.n_total >= 500:
        tags.append("high-volume")
    if b.n_block > 0 and (b.n_block / b.n_total) > 0.05:
        tags.append("unstable")
    if b.total_cost_usd > 1.0:
        tags.append("high-cost")
    if b.n_approval > 0 and (b.n_approval / b.n_total) > 0.10:
        tags.append("frequent-approvals")

    return KnowledgeEntry(
        entry_id=make_entry_id(EntryKind.AGENT, b.aid),
        kind=EntryKind.AGENT,
        title=f"Agent {b.aid}",
        summary=summary,
        infobox=infobox,
        sections=tuple(sections),
        related=tuple(related),
        tags=tuple(tags),
        ts_first_ns=b.ts_first_ns,
        ts_last_ns=b.ts_last_ns,
        n_observations=b.n_total,
        confidence=_confidence_from_n(b.n_total),
    )


# ──────────────────────────────────────────────────────────────────
# TOOL entry rendering
# ──────────────────────────────────────────────────────────────────


def _render_tool_entry(b: _ToolBucket) -> KnowledgeEntry:
    """One wiki article per tool. Sections: usage, stability,
    patterns. Cross-refs the top patterns + top agents."""
    top_patterns = b.pattern_counts.most_common(TOP_K_PATTERNS_PER_TOOL)
    top_blocks = b.block_reason_counts.most_common(3)
    top_aids = b.aid_counts.most_common(3)

    avg_latency = (b.sum_latency_ms / b.n_total) if b.n_total > 0 else 0.0
    avg_cost = (b.sum_cost_usd / b.n_total) if b.n_total > 0 else 0.0
    block_rate = _pct(b.n_block, b.n_total)
    approval_rate = _pct(b.n_approval, b.n_total)

    summary = (
        f"{b.n_total:,} invocations in window: "
        f"{_pct(b.n_allow, b.n_total)} ALLOW, {approval_rate} "
        f"REQUIRE_APPROVAL, {block_rate} BLOCK. Average latency "
        f"{avg_latency:.1f}ms."
    )

    infobox = InfoBox(fields={
        "n_calls": b.n_total,
        "n_allow": b.n_allow,
        "n_require_approval": b.n_approval,
        "n_block": b.n_block,
        "total_cost_usd": round(b.sum_cost_usd, 4),
        "avg_cost_per_call_usd": round(avg_cost, 6),
        "avg_latency_ms": round(avg_latency, 2),
        "first_seen": _fmt_ns(b.ts_first_ns),
        "last_seen": _fmt_ns(b.ts_last_ns),
    })

    sections: list[Section] = []

    stability_parts = [
        f"- BLOCK rate: **{block_rate}** ({b.n_block:,} events)",
        f"- REQUIRE_APPROVAL rate: {approval_rate} ({b.n_approval:,} events)",
    ]
    if top_blocks:
        stability_parts.append("")
        stability_parts.append("**Top BLOCK reasons**:")
        for reason, n in top_blocks:
            stability_parts.append(f"- {reason!r}: {n}")
    sections.append(Section(
        heading="Stability",
        body="\n".join(stability_parts),
    ))

    if top_patterns:
        pat_lines = [
            f"- `{sig}`: {n} fires"
            for sig, n in top_patterns
        ]
        sections.append(Section(
            heading="Patterns",
            body=(
                "REQUIRE_APPROVAL patterns that fire on this tool:\n\n"
                + "\n".join(pat_lines)
            ),
        ))

    if top_aids:
        sections.append(Section(
            heading="Top users",
            body="\n".join(
                f"- agent/{aid}: {n:,} calls" for aid, n in top_aids
            ),
        ))

    related: list[str] = []
    related.extend(
        make_entry_id(EntryKind.PATTERN, f"{b.tool}:{sig}")
        for sig, _ in top_patterns
    )
    related.extend(
        make_entry_id(EntryKind.AGENT, aid) for aid, _ in top_aids
    )

    tags: list[str] = []
    if b.n_total >= 200:
        tags.append("high-volume")
    if b.n_block > 0 and (b.n_block / b.n_total) > 0.05:
        tags.append("unstable")
    if b.sum_cost_usd > 0.50:
        tags.append("high-cost")

    return KnowledgeEntry(
        entry_id=make_entry_id(EntryKind.TOOL, b.tool),
        kind=EntryKind.TOOL,
        title=f"Tool {b.tool}",
        summary=summary,
        infobox=infobox,
        sections=tuple(sections),
        related=tuple(related),
        tags=tuple(tags),
        ts_first_ns=b.ts_first_ns,
        ts_last_ns=b.ts_last_ns,
        n_observations=b.n_total,
        confidence=_confidence_from_n(b.n_total),
    )


# ──────────────────────────────────────────────────────────────────
# PATTERN entry rendering
# ──────────────────────────────────────────────────────────────────


def _render_pattern_entry(b: _PatternBucket) -> KnowledgeEntry:
    """One wiki article per (tool, reason_signature). The unit of
    aggregation the autonomy learner uses, so this entry is also
    a natural place to surface auto-approval rates."""
    top_aids = b.aid_counts.most_common(TOP_K_AGENTS_PER_PATTERN)
    bypass_rate = _pct(b.n_bypass_applied, b.n_fires)
    block_rate = _pct(b.n_block_followup, b.n_fires)

    summary = (
        f"REQUIRE_APPROVAL signature `{b.signature}` on `{b.tool}`: "
        f"{b.n_fires} fires; {bypass_rate} auto-approved, "
        f"{block_rate} followed by BLOCK."
    )

    infobox = InfoBox(fields={
        "tool": b.tool,
        "reason_signature": b.signature,
        "n_fires": b.n_fires,
        "n_bypass_applied": b.n_bypass_applied,
        "n_block_followup": b.n_block_followup,
        "bypass_rate": bypass_rate,
        "block_followup_rate": block_rate,
        "first_seen": _fmt_ns(b.ts_first_ns),
        "last_seen": _fmt_ns(b.ts_last_ns),
    })

    sections: list[Section] = []
    if b.sample_reasons:
        sections.append(Section(
            heading="Sample reasons",
            body="\n".join(f"- {r!r}" for r in b.sample_reasons),
        ))

    outcomes_body = (
        f"- Total fires: **{b.n_fires}**\n"
        f"- Auto-approved by autonomy bypass: {b.n_bypass_applied} "
        f"({bypass_rate})\n"
        f"- Followed by BLOCK within 10 calls: {b.n_block_followup} "
        f"({block_rate})\n"
        f"- Cost while firing: ${b.sum_cost_usd:.4f}"
    )
    sections.append(Section(heading="Outcomes", body=outcomes_body))

    if top_aids:
        sections.append(Section(
            heading="Top agents",
            body="\n".join(
                f"- agent/{aid}: {n} fires" for aid, n in top_aids
            ),
        ))

    related: list[str] = [
        make_entry_id(EntryKind.TOOL, b.tool),
    ]
    related.extend(
        make_entry_id(EntryKind.AGENT, aid) for aid, _ in top_aids
    )

    tags: list[str] = []
    if b.n_fires >= 50:
        tags.append("frequent")
    if b.n_block_followup > 0 and (b.n_block_followup / b.n_fires) > 0.05:
        tags.append("risky-bypass")
    if b.n_bypass_applied > 0:
        tags.append("autonomy-active")
    if b.signature.startswith("rule:"):
        tags.append("rule-fired")

    return KnowledgeEntry(
        entry_id=make_entry_id(
            EntryKind.PATTERN, f"{b.tool}:{b.signature}",
        ),
        kind=EntryKind.PATTERN,
        title=f"Pattern {b.signature} on {b.tool}",
        summary=summary,
        infobox=infobox,
        sections=tuple(sections),
        related=tuple(related),
        tags=tuple(tags),
        ts_first_ns=b.ts_first_ns,
        ts_last_ns=b.ts_last_ns,
        n_observations=b.n_fires,
        confidence=_confidence_from_n(b.n_fires),
    )


# ──────────────────────────────────────────────────────────────────
# v0.5.20 — SESSION entries (gap-segmented contiguous activity)
# ──────────────────────────────────────────────────────────────────


def _segment_sessions(
    aid_timeline: list[ContextMemoryRecord],
    *,
    gap_seconds: int = SESSION_GAP_SECONDS,
) -> list[list[ContextMemoryRecord]]:
    """Split a per-aid timeline into sessions wherever the gap
    between consecutive calls exceeds ``gap_seconds``."""
    if not aid_timeline:
        return []
    gap_ns = gap_seconds * 1_000_000_000
    sessions: list[list[ContextMemoryRecord]] = []
    current: list[ContextMemoryRecord] = [aid_timeline[0]]
    for prev, nxt in zip(aid_timeline, aid_timeline[1:], strict=False):
        if nxt.ts_ns - prev.ts_ns > gap_ns:
            sessions.append(current)
            current = [nxt]
        else:
            current.append(nxt)
    sessions.append(current)
    return sessions


def _render_session_entry(
    aid: str,
    session: list[ContextMemoryRecord],
) -> KnowledgeEntry | None:
    """One wiki article per coherent burst of calls. Skipped when
    the session is short AND uneventful."""
    n_total = len(session)
    n_block = sum(1 for r in session if r.decision == "BLOCK")
    if n_total < SESSION_MIN_CALLS and n_block == 0:
        return None

    n_allow = sum(1 for r in session if r.decision == "ALLOW")
    n_approval = sum(1 for r in session if r.decision == "REQUIRE_APPROVAL")
    tool_counts: Counter[str] = Counter(r.tool_name for r in session if r.tool_name)
    ts_first = session[0].ts_ns
    ts_last = session[-1].ts_ns
    duration_s = max(0, (ts_last - ts_first) // 1_000_000_000)
    total_cost = sum(float(r.cost_usd or 0.0) for r in session)
    top_tools = tool_counts.most_common(3)

    # Use ts_first_ns as the stable slug — same session always
    # produces the same entry_id across re-builds.
    slug = f"{aid}/{ts_first}"

    summary = (
        f"Session for agent {aid} spanning "
        f"{_fmt_ns(ts_first)}-{_fmt_ns(ts_last)}: "
        f"{n_total} call(s) over {duration_s // 60}m, "
        f"{n_block} BLOCK, {n_approval} REQUIRE_APPROVAL, "
        f"cost ${total_cost:.4f}."
    )
    infobox = InfoBox(fields={
        "aid": aid,
        "ts_first": _fmt_ns(ts_first),
        "ts_last": _fmt_ns(ts_last),
        "duration_min": int(duration_s // 60),
        "n_calls": n_total,
        "n_allow": n_allow,
        "n_require_approval": n_approval,
        "n_block": n_block,
        "total_cost_usd": round(total_cost, 4),
        "top_tool": top_tools[0][0] if top_tools else "(none)",
    })

    sections: list[Section] = []
    if top_tools:
        sections.append(Section(
            heading="Top tools",
            body="\n".join(
                f"- `{t}`: {n} calls" for t, n in top_tools
            ),
        ))
    if n_block > 0:
        block_records = [r for r in session if r.decision == "BLOCK"][:3]
        sections.append(Section(
            heading="BLOCK events",
            body="\n".join(
                f"- `{r.tool_name}` at {_fmt_ns(r.ts_ns)}: "
                f"{(r.reason or '')[:80]!r}"
                for r in block_records
            ),
        ))

    related: list[str] = [make_entry_id(EntryKind.AGENT, aid)]
    related.extend(
        make_entry_id(EntryKind.TOOL, t) for t, _ in top_tools
    )

    tags: list[str] = []
    if n_block > 0:
        tags.append("had-block")
    if total_cost > 0.10:
        tags.append("high-cost-session")
    if duration_s > 3600:
        tags.append("long-session")

    return KnowledgeEntry(
        entry_id=make_entry_id(EntryKind.SESSION, slug),
        kind=EntryKind.SESSION,
        title=f"Session {_fmt_ns(ts_first)} ({aid})",
        summary=summary,
        infobox=infobox,
        sections=tuple(sections),
        related=tuple(related),
        tags=tuple(tags),
        ts_first_ns=ts_first,
        ts_last_ns=ts_last,
        n_observations=n_total,
        confidence=_confidence_from_n(n_total),
    )


# ──────────────────────────────────────────────────────────────────
# v0.5.20 — INCIDENT entries (BLOCK-anchored)
# ──────────────────────────────────────────────────────────────────


def _render_incident_entry(
    aid: str,
    block_record: ContextMemoryRecord,
    aid_timeline: list[ContextMemoryRecord],
) -> KnowledgeEntry | None:
    """One wiki article per BLOCK + its surrounding context. The
    setup window (preceding calls) is what enables the agent
    that hit the BLOCK to learn the precursor pattern; the
    recovery window (succeeding calls) shows how the agent
    self-corrected (if at all)."""
    if block_record.decision != "BLOCK":
        return None
    try:
        idx = aid_timeline.index(block_record)
    except ValueError:
        return None

    setup = aid_timeline[max(0, idx - INCIDENT_BEFORE) : idx]
    recovery = aid_timeline[idx + 1 : idx + 1 + INCIDENT_AFTER]
    recovered = (
        len(recovery) > 0
        and all(r.decision != "BLOCK" for r in recovery)
    )

    slug = f"{aid}/{block_record.trace_id}"
    reason_short = (block_record.reason or "(no reason)")[:80]
    summary = (
        f"BLOCK on `{block_record.tool_name}` at "
        f"{_fmt_ns(block_record.ts_ns)}: {reason_short!r}. "
        f"Preceded by {len(setup)} setup call(s); "
        f"{'recovered' if recovered else 'no recovery observed'} "
        f"in the next {len(recovery)} call(s)."
    )

    infobox = InfoBox(fields={
        "aid": aid,
        "trace_id": block_record.trace_id,
        "tool": block_record.tool_name,
        "reason": reason_short,
        "ts": _fmt_ns(block_record.ts_ns),
        "n_setup_calls": len(setup),
        "n_recovery_calls": len(recovery),
        "recovered": recovered,
    })

    sections: list[Section] = []
    if setup:
        sections.append(Section(
            heading="Setup (preceding calls)",
            body="\n".join(
                f"{i + 1}. `{r.tool_name}` "
                f"[{r.decision}] {(r.reason or '')[:60]!r}"
                for i, r in enumerate(setup)
            ),
        ))
    sections.append(Section(
        heading="BLOCK",
        body=(
            f"- tool: `{block_record.tool_name}`\n"
            f"- reason: {block_record.reason or '(none)'}\n"
            f"- ts: {_fmt_ns(block_record.ts_ns)}"
        ),
    ))
    if recovery:
        sections.append(Section(
            heading="Recovery (subsequent calls)",
            body="\n".join(
                f"{i + 1}. `{r.tool_name}` "
                f"[{r.decision}] {(r.reason or '')[:60]!r}"
                for i, r in enumerate(recovery)
            ),
        ))

    related: list[str] = [
        make_entry_id(EntryKind.AGENT, aid),
        make_entry_id(EntryKind.TOOL, block_record.tool_name),
    ]
    sig = reason_signature(block_record.reason or "")
    related.append(
        make_entry_id(
            EntryKind.PATTERN, f"{block_record.tool_name}:{sig}",
        )
    )

    tags: list[str] = ["block"]
    if recovered:
        tags.append("recovered")
    else:
        tags.append("unresolved")
    if sig.startswith("rule:") or sig in ("dangerous_pattern", "sensitive_path"):
        tags.append("safety-trigger")

    return KnowledgeEntry(
        entry_id=make_entry_id(EntryKind.INCIDENT, slug),
        kind=EntryKind.INCIDENT,
        title=f"Incident on {block_record.tool_name} ({block_record.trace_id[:8]})",
        summary=summary,
        infobox=infobox,
        sections=tuple(sections),
        related=tuple(related),
        tags=tuple(tags),
        ts_first_ns=setup[0].ts_ns if setup else block_record.ts_ns,
        ts_last_ns=(
            recovery[-1].ts_ns if recovery else block_record.ts_ns
        ),
        n_observations=1 + len(setup) + len(recovery),
        confidence=0.9,  # incidents are concrete singletons — high confidence
    )


# ──────────────────────────────────────────────────────────────────
# v0.5.20 — WORKFLOW entries (recurring tool sequences)
# ──────────────────────────────────────────────────────────────────


def _mine_workflows(
    aid: str,
    aid_timeline: list[ContextMemoryRecord],
    *,
    seq_len: int = WORKFLOW_SEQUENCE_LEN,
    min_occurrences: int = WORKFLOW_MIN_OCCURRENCES,
) -> list[KnowledgeEntry]:
    """Return one WORKFLOW entry per recurring tool n-gram.

    A "workflow" is a sequence of ``seq_len`` consecutive
    tool_names that recurs at least ``min_occurrences`` times in
    the agent's timeline. Sequences that include unnamed /
    sidechain tools are skipped (would dominate the histogram
    with low-information patterns)."""
    tool_seq = [r.tool_name for r in aid_timeline if r.tool_name]
    if len(tool_seq) < seq_len:
        return []

    counts: Counter[tuple[str, ...]] = Counter()
    sample_ts: dict[tuple[str, ...], int] = {}
    for i in range(len(tool_seq) - seq_len + 1):
        seq = tuple(tool_seq[i : i + seq_len])
        counts[seq] += 1
        sample_ts.setdefault(seq, aid_timeline[i].ts_ns)

    out: list[KnowledgeEntry] = []
    for seq, n_occ in counts.items():
        if n_occ < min_occurrences:
            continue
        slug = f"{aid}/{'-'.join(seq)}"
        seq_display = " → ".join(f"`{t}`" for t in seq)
        summary = (
            f"Workflow {seq_display} recurs {n_occ} times for "
            f"agent {aid}."
        )
        infobox = InfoBox(fields={
            "aid": aid,
            "sequence": " -> ".join(seq),
            "sequence_length": seq_len,
            "n_occurrences": n_occ,
            "first_seen": _fmt_ns(sample_ts[seq]),
        })
        related: list[str] = [make_entry_id(EntryKind.AGENT, aid)]
        related.extend(make_entry_id(EntryKind.TOOL, t) for t in seq)

        tags: list[str] = ["recurring-workflow"]
        if n_occ >= 10:
            tags.append("dominant-workflow")

        out.append(KnowledgeEntry(
            entry_id=make_entry_id(EntryKind.WORKFLOW, slug),
            kind=EntryKind.WORKFLOW,
            title=f"Workflow {' → '.join(seq)} ({aid})",
            summary=summary,
            infobox=infobox,
            sections=(),
            related=tuple(related),
            tags=tuple(tags),
            ts_first_ns=sample_ts[seq],
            ts_last_ns=aid_timeline[-1].ts_ns,
            n_observations=n_occ,
            confidence=_confidence_from_n(n_occ * 10),  # n=5 hits → conf 1.0
        ))
    return out


__all__ = [
    "CONFIDENCE_FULL_AT",
    "INCIDENT_AFTER",
    "INCIDENT_BEFORE",
    "SESSION_GAP_SECONDS",
    "SESSION_MIN_CALLS",
    "TOP_K_AGENTS_PER_PATTERN",
    "TOP_K_PATTERNS_PER_TOOL",
    "TOP_K_TOOLS_PER_AGENT",
    "WORKFLOW_MIN_OCCURRENCES",
    "WORKFLOW_SEQUENCE_LEN",
    "build_knowledge",
]
