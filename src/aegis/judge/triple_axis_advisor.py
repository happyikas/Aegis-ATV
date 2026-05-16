"""TripleAxisAdvisor — ATV-driven sLLM scene interpretation (v0.5.10).

Closes the gap identified in the v0.5.9 self-audit: the existing
sLLM judge path produces a 3-class verdict (ALLOW/BLOCK/
REQUIRE_APPROVAL) but does **not** interpret the run-time context
into the three axes the patent's advisor pipeline targets:

    1. **Token efficiency**  — is the agent wasting tokens?
    2. **Cache performance** — is the KV cache hit rate healthy?
    3. **Stability**         — is the agent stuck / triggering
                               safety risks?

This module asks the sLLM to read a compact ATV-derived summary
of the recent window and produce a structured assessment **per
axis** — each carrying a score, severity, one-line interpretation,
and (when applicable) a concrete next-action recommendation.

The result is a :class:`TripleAxisAdvice` dataclass that:

* Can be rendered as a per-axis terminal report.
* Can be serialised to JSON for piping into CI / dashboards.
* Carries `advisor_kind = "sllm" | "heuristic"` for replay /
  forensics so audit can pin assessments to the composer that
  produced them.

The heuristic path always runs first as a baseline. The sLLM path
(opt-in via :data:`AEGIS_TRIPLE_AXIS_PROVIDER=sllm` or explicit
``prefer_sllm=True``) refines the prose fields per axis using the
same defensive-fallback contract as the v0.5.9 ActionAdvice sLLM
brain. The decision-class fields stay heuristic so audit replay
remains deterministic.

This module does NOT change existing surfaces — it adds a new one.
The legacy ``compose_advice`` / ``compose_advice_heuristic`` paths
are untouched, so v0.5.9 audit replay byte-matches.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

from aegis.context_memory.record import ContextMemoryRecord

# Marker version → hash for advisor_hash replay pinning.
_TRIPLE_AXIS_VERSION: Final[str] = "triple_axis_advisor_v1"
_TRIPLE_AXIS_HASH: Final[str] = hashlib.sha3_256(
    _TRIPLE_AXIS_VERSION.encode(),
).hexdigest()


# ──────────────────────────────────────────────────────────────────
# Output shape
# ──────────────────────────────────────────────────────────────────


Axis = Literal["token_efficiency", "cache_performance", "stability"]
Severity = Literal["ok", "warn", "alert"]
AdvisorKind = Literal["heuristic", "sllm"]


@dataclass(frozen=True)
class AxisAssessment:
    """One axis's interpretation. ``score`` is in [0.0, 1.0] — 1.0
    means ideal, 0.0 means catastrophically bad. Severity is the
    bucketed view: ok (≥0.7), warn (0.4 .. 0.7), alert (<0.4)."""

    axis: Axis
    score: float
    severity: Severity
    interpretation: str
    next_action: str | None = None
    cited_signals: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TripleAxisAdvice:
    """Cross-axis assessment of one ContextMemory window.

    ``overall_priority`` is the axis with the lowest score (worst
    state) — operators triage by reading this single field. ``summary``
    is a one-line cross-axis synthesis the sLLM (or heuristic) writes
    once it has all three assessments in hand.
    """

    token_efficiency: AxisAssessment
    cache_performance: AxisAssessment
    stability: AxisAssessment
    overall_priority: Axis
    summary: str
    n_records: int
    window_seconds: int
    advisor_kind: AdvisorKind = "heuristic"
    advisor_hash: str = _TRIPLE_AXIS_HASH
    produced_at_ns: int = 0


# ──────────────────────────────────────────────────────────────────
# Signal extraction — compact ATV-derived features per axis
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AxisSignals:
    """Per-axis numeric signals extracted from a ContextMemory
    window. The heuristic assessor reads these directly; the sLLM
    assessor renders them into a prompt and asks for interpretation.
    Carrying both surfaces from the same struct keeps the two
    paths sharing the same ground truth."""

    # Window summary
    n_total: int
    n_allow: int
    n_block: int
    n_approval: int
    window_seconds: int

    # Token-efficiency signals
    total_cost_usd: float
    median_cost_per_call: float
    p95_cost_per_call: float
    top_cost_tool: str
    top_cost_tool_total: float
    repeat_call_ratio: float    # fraction of calls that are part of a
                                # 3+ repetition pattern
    avg_tokens_per_call: int

    # Cache-performance signals
    estimated_cache_hit_rate: float   # inferred from repeat patterns
    prefix_instability_count: int     # turns where prompt structure
                                       # diverged sharply
    redundant_read_count: int          # same Read tool_args within
                                       # the window

    # Stability signals
    block_rate: float
    approval_rate: float
    loop_detected_count: int
    rule_violation_count: int
    dangerous_pattern_count: int
    sensitive_path_count: int


def extract_axis_signals(
    records: Iterable[ContextMemoryRecord],
    *,
    window_seconds: int = 7 * 86400,
) -> AxisSignals:
    """Compute the per-axis signals from a ContextMemory window.

    Pure function; identical input → identical output. The sLLM
    assessor's prompt is built deterministically from these
    numbers, so the same window produces the same prompt regardless
    of when it's called.
    """
    rec_list = list(records)
    n_total = len(rec_list)
    if n_total == 0:
        return _empty_signals(window_seconds)

    # Decisions
    n_allow = sum(1 for r in rec_list if r.decision == "ALLOW")
    n_block = sum(1 for r in rec_list if r.decision == "BLOCK")
    n_approval = sum(1 for r in rec_list if r.decision == "REQUIRE_APPROVAL")

    # Cost — sorted for p95
    allow_costs = sorted(r.cost_usd for r in rec_list if r.decision == "ALLOW")
    total_cost = sum(allow_costs)
    median_cost = (
        allow_costs[len(allow_costs) // 2] if allow_costs else 0.0
    )
    p95_cost = (
        allow_costs[max(0, int(len(allow_costs) * 0.95) - 1)]
        if allow_costs else 0.0
    )

    # Top-cost tool
    by_tool: Counter[str] = Counter()
    for r in rec_list:
        if r.decision == "ALLOW":
            by_tool[r.tool_name] += r.cost_usd  # type: ignore[assignment]
    top_tool, top_tool_total = (
        by_tool.most_common(1)[0] if by_tool else ("(none)", 0.0)
    )

    # Repeat-call ratio — fraction of records in a (aid, tool, args-hash)
    # bucket of size ≥3. Args-hash is approximated by reason string +
    # trace prefix since the raw args aren't in CM.
    bucket: Counter[tuple[str, str]] = Counter()
    for r in rec_list:
        bucket[(r.aid, r.tool_name)] += 1
    in_repeat = sum(c for c in bucket.values() if c >= 3)
    repeat_ratio = in_repeat / n_total if n_total else 0.0

    # Tokens (cost ≈ tokens × per-token rate; we recover token count
    # from the record directly when present, fall back to cost-based
    # estimate using Sonnet 3.5 input pricing).
    token_estimates = []
    for r in rec_list:
        toks = r.tokens_in + r.tokens_out
        if toks > 0:
            token_estimates.append(toks)
        elif r.cost_usd > 0:
            # ~$3/1M input + $15/1M output, ratio ~2:1 → effective
            # ~$7/1M weighted. Reverse: tokens ≈ cost / $7e-6.
            token_estimates.append(int(r.cost_usd / 7e-6))
    avg_tokens = (
        sum(token_estimates) // len(token_estimates)
        if token_estimates else 0
    )

    # Cache estimation — heuristic: high repeat_ratio with no caching
    # signal implies cache_hit_rate is low. Bounded [0.0, 1.0].
    est_cache_hit = max(0.0, 1.0 - (repeat_ratio * 1.5))

    # Prefix instability — proxy: count records where step_traces
    # contains a "step336" loop reason (means the agent's prompt
    # structure isn't stable enough for KV cache reuse).
    prefix_unstable = sum(
        1 for r in rec_list
        if "repeated" in (r.reason or "")
    )

    # Redundant Read count — repeat-call bucket count for Read tool.
    redundant_reads = sum(
        c - 1 for (_, tool), c in bucket.items()
        if tool == "Read" and c >= 2
    )

    # Stability signals
    loop_count = sum(
        1 for r in rec_list
        if "repeated" in (r.reason or "")
    )
    rule_count = sum(
        1 for r in rec_list
        if (r.reason or "").startswith("rule:")
    )
    danger_count = sum(
        1 for r in rec_list
        if (r.reason or "").startswith("dangerous pattern")
    )
    sensitive_count = sum(
        1 for r in rec_list
        if "sensitive path" in (r.reason or "")
    )

    return AxisSignals(
        n_total=n_total,
        n_allow=n_allow,
        n_block=n_block,
        n_approval=n_approval,
        window_seconds=window_seconds,
        total_cost_usd=total_cost,
        median_cost_per_call=median_cost,
        p95_cost_per_call=p95_cost,
        top_cost_tool=top_tool,
        top_cost_tool_total=float(top_tool_total),
        repeat_call_ratio=repeat_ratio,
        avg_tokens_per_call=avg_tokens,
        estimated_cache_hit_rate=est_cache_hit,
        prefix_instability_count=prefix_unstable,
        redundant_read_count=redundant_reads,
        block_rate=n_block / n_total if n_total else 0.0,
        approval_rate=n_approval / n_total if n_total else 0.0,
        loop_detected_count=loop_count,
        rule_violation_count=rule_count,
        dangerous_pattern_count=danger_count,
        sensitive_path_count=sensitive_count,
    )


def _empty_signals(window_seconds: int) -> AxisSignals:
    """Signal struct for an empty window — every axis defaults to
    ok/1.0 so the assessor produces a clean 'no data' response."""
    return AxisSignals(
        n_total=0, n_allow=0, n_block=0, n_approval=0,
        window_seconds=window_seconds,
        total_cost_usd=0.0, median_cost_per_call=0.0,
        p95_cost_per_call=0.0, top_cost_tool="(none)",
        top_cost_tool_total=0.0, repeat_call_ratio=0.0,
        avg_tokens_per_call=0, estimated_cache_hit_rate=1.0,
        prefix_instability_count=0, redundant_read_count=0,
        block_rate=0.0, approval_rate=0.0,
        loop_detected_count=0, rule_violation_count=0,
        dangerous_pattern_count=0, sensitive_path_count=0,
    )


# ──────────────────────────────────────────────────────────────────
# Heuristic assessor — always-available baseline
# ──────────────────────────────────────────────────────────────────


def _bucket_severity(score: float) -> Severity:
    if score >= 0.7:
        return "ok"
    if score >= 0.4:
        return "warn"
    return "alert"


def _assess_token_efficiency_heuristic(
    s: AxisSignals,
) -> AxisAssessment:
    """Score = 1.0 - 0.5 × repeat_ratio - 0.3 × (top_tool_cost /
    total) - 0.2 × (avg_tokens / 50_000). Clipped to [0, 1]."""
    if s.n_total == 0:
        return AxisAssessment(
            axis="token_efficiency", score=1.0, severity="ok",
            interpretation="No agent traffic in window.",
            cited_signals=(),
        )
    top_tool_share = (
        s.top_cost_tool_total / s.total_cost_usd
        if s.total_cost_usd > 0 else 0.0
    )
    tok_penalty = min(1.0, s.avg_tokens_per_call / 50_000.0)
    score = max(0.0, min(1.0,
        1.0 - 0.5 * s.repeat_call_ratio
            - 0.3 * top_tool_share
            - 0.2 * tok_penalty,
    ))
    sev = _bucket_severity(score)
    parts = []
    if s.repeat_call_ratio > 0.3:
        parts.append(
            f"{s.repeat_call_ratio * 100:.0f}% of calls are in a 3+ "
            "repeat pattern (cache should have hit but didn't)"
        )
    if top_tool_share > 0.5 and s.top_cost_tool != "(none)":
        parts.append(
            f"`{s.top_cost_tool}` is "
            f"{top_tool_share * 100:.0f}% of total cost "
            f"(${s.top_cost_tool_total:.4f}) — single hot tool"
        )
    if s.avg_tokens_per_call > 20_000:
        parts.append(
            f"average tokens/call = {s.avg_tokens_per_call:,} — "
            "calls are large (likely over-scoped Reads)"
        )
    interpretation = (
        "; ".join(parts) if parts
        else "Token usage within budget; no obvious waste pattern."
    )
    action: str | None
    if sev == "alert":
        action = (
            f"Audit the top spender (`{s.top_cost_tool}`) — "
            "add caching / batching / scoped reads."
        )
    elif sev == "warn":
        action = (
            "Run `aegis memory claude-md` to surface concrete "
            "edit proposals for the wasteful tools."
        )
    else:
        action = None
    cited = tuple(
        x for x in (
            "repeat_call_ratio" if s.repeat_call_ratio > 0.2 else "",
            "top_cost_tool" if top_tool_share > 0.4 else "",
            "avg_tokens_per_call" if s.avg_tokens_per_call > 10_000 else "",
        )
        if x
    )
    return AxisAssessment(
        axis="token_efficiency", score=score, severity=sev,
        interpretation=interpretation,
        next_action=action,
        cited_signals=cited,
    )


def _assess_cache_performance_heuristic(
    s: AxisSignals,
) -> AxisAssessment:
    """Score from estimated cache hit rate + prefix instability."""
    if s.n_total == 0:
        return AxisAssessment(
            axis="cache_performance", score=1.0, severity="ok",
            interpretation="No agent traffic in window.",
            cited_signals=(),
        )
    prefix_penalty = min(0.5, s.prefix_instability_count / max(s.n_total, 1))
    score = max(0.0, min(1.0, s.estimated_cache_hit_rate - prefix_penalty))
    sev = _bucket_severity(score)
    parts = []
    parts.append(
        f"estimated cache hit rate ~{s.estimated_cache_hit_rate * 100:.0f}%"
    )
    if s.redundant_read_count > 0:
        parts.append(
            f"{s.redundant_read_count} redundant Read calls "
            "(same tool + agent) detected"
        )
    if s.prefix_instability_count > 0:
        parts.append(
            f"{s.prefix_instability_count} prompt-prefix instability events "
            "(step336 loop reasons)"
        )
    interpretation = "; ".join(parts) if parts else (
        "Cache utilisation looks healthy."
    )
    action: str | None
    if sev == "alert":
        action = (
            "Stabilise the prompt prefix and dedupe Read calls in "
            "CLAUDE.md to recover cache hits."
        )
    elif sev == "warn":
        action = (
            "Some cache thrash detected. Consider prompt-prefix "
            "stabilisation."
        )
    else:
        action = None
    cited = tuple(
        x for x in (
            "estimated_cache_hit_rate" if s.estimated_cache_hit_rate < 0.7 else "",
            "redundant_read_count" if s.redundant_read_count > 0 else "",
            "prefix_instability_count" if s.prefix_instability_count > 0 else "",
        )
        if x
    )
    return AxisAssessment(
        axis="cache_performance", score=score, severity=sev,
        interpretation=interpretation,
        next_action=action,
        cited_signals=cited,
    )


def _assess_stability_heuristic(s: AxisSignals) -> AxisAssessment:
    """Stability = 1.0 - block_rate - 0.5×approval_rate - loop penalty."""
    if s.n_total == 0:
        return AxisAssessment(
            axis="stability", score=1.0, severity="ok",
            interpretation="No agent traffic in window.",
            cited_signals=(),
        )
    loop_penalty = min(0.4, s.loop_detected_count / max(s.n_total, 1))
    danger_penalty = min(0.3, (s.dangerous_pattern_count + s.sensitive_path_count) / max(s.n_total, 1))
    score = max(0.0, min(1.0,
        1.0 - s.block_rate - 0.3 * s.approval_rate - loop_penalty - danger_penalty,
    ))
    sev = _bucket_severity(score)
    parts = []
    if s.block_rate > 0.01:
        parts.append(
            f"{s.block_rate * 100:.1f}% BLOCK rate "
            f"({s.n_block} of {s.n_total})"
        )
    if s.loop_detected_count > 0:
        parts.append(
            f"{s.loop_detected_count} step336 loop events"
        )
    if s.dangerous_pattern_count > 0:
        parts.append(
            f"{s.dangerous_pattern_count} dangerous-pattern hits"
        )
    if s.sensitive_path_count > 0:
        parts.append(
            f"{s.sensitive_path_count} sensitive-path approvals"
        )
    if s.rule_violation_count > 0:
        parts.append(
            f"{s.rule_violation_count} custom-rule matches"
        )
    interpretation = "; ".join(parts) if parts else (
        "Agent is operating cleanly — no destructive patterns, no loops."
    )
    action: str | None
    if sev == "alert":
        action = (
            "Inspect recent BLOCKs with `aegis forensic last`. "
            "Loop / drift events should be documented in CLAUDE.md "
            "via `memory claude-md`."
        )
    elif sev == "warn":
        action = (
            "Surface loop / drift via `aegis memory claude-md` and "
            "add the proposed guardrails."
        )
    else:
        action = None
    cited = tuple(
        x for x in (
            "block_rate" if s.block_rate > 0.005 else "",
            "loop_detected_count" if s.loop_detected_count > 0 else "",
            "dangerous_pattern_count" if s.dangerous_pattern_count > 0 else "",
            "sensitive_path_count" if s.sensitive_path_count > 0 else "",
        )
        if x
    )
    return AxisAssessment(
        axis="stability", score=score, severity=sev,
        interpretation=interpretation,
        next_action=action,
        cited_signals=cited,
    )


def assess_via_heuristic(s: AxisSignals) -> TripleAxisAdvice:
    """Compose the three axis assessments + cross-axis summary
    deterministically from signals. Always available; sub-millisecond
    on any window size."""
    t = _assess_token_efficiency_heuristic(s)
    c = _assess_cache_performance_heuristic(s)
    st = _assess_stability_heuristic(s)
    # Worst axis = overall priority. Ties broken in declared order
    # (token > cache > stability) — matches the patent ordering of
    # cost/cache/security advisors.
    scores = [(t.score, "token_efficiency", t),
              (c.score, "cache_performance", c),
              (st.score, "stability", st)]
    scores.sort(key=lambda x: x[0])
    overall: Axis = scores[0][1]   # type: ignore[assignment]
    summary = _compose_cross_axis_summary(t, c, st, s)
    return TripleAxisAdvice(
        token_efficiency=t,
        cache_performance=c,
        stability=st,
        overall_priority=overall,
        summary=summary,
        n_records=s.n_total,
        window_seconds=s.window_seconds,
        advisor_kind="heuristic",
        advisor_hash=_TRIPLE_AXIS_HASH,
        produced_at_ns=time.time_ns(),
    )


def _compose_cross_axis_summary(
    t: AxisAssessment,
    c: AxisAssessment,
    st: AxisAssessment,
    s: AxisSignals,
) -> str:
    """One-line operator-readable synthesis. Heuristic always
    produces a sentence; sLLM enhancer may refine it."""
    if s.n_total == 0:
        return "No traffic in window — nothing to assess."
    alerts = [a.axis for a in (t, c, st) if a.severity == "alert"]
    warns = [a.axis for a in (t, c, st) if a.severity == "warn"]
    if not alerts and not warns:
        return f"All three axes healthy across {s.n_total} calls."
    if alerts:
        return (
            f"{len(alerts)} axis in alert ({', '.join(alerts)}); "
            f"focus there first."
        )
    return (
        f"{len(warns)} axis in warn ({', '.join(warns)}); "
        f"monitor + run `aegis memory claude-md` for fixes."
    )


# ──────────────────────────────────────────────────────────────────
# sLLM assessor — opt-in scene interpretation
# ──────────────────────────────────────────────────────────────────


def _build_sllm_prompt(
    s: AxisSignals,
    baseline: TripleAxisAdvice,
    *,
    knowledge_context: str | None = None,
) -> str:
    """Render the AxisSignals + heuristic baseline into a tight
    prompt asking the sLLM to refine the per-axis prose. The
    heuristic baseline is included so the sLLM doesn't have to
    reinvent the wheel — its job is *scene interpretation*, not
    score derivation.

    v0.5.16: when ``knowledge_context`` is provided (a markdown
    block from :func:`aegis.knowledge.knowledge_context_for_advisor`),
    it's spliced in between the instructions and the signals so the
    sLLM reads "what we know about the agent's normal behavior" then
    "what just happened" then "the heuristic baseline" before
    composing its response. The context grounds the assessment in
    history rather than the current window alone."""
    knowledge_block = ""
    if knowledge_context:
        knowledge_block = (
            "\n=== Knowledge context (agent background) ===\n"
            f"{knowledge_context.rstrip()}\n"
            "=== End knowledge context ===\n"
        )
    return (
        "You are reading a compact summary of an AI agent's recent "
        "tool-call window. Assess the situation across THREE axes:\n"
        "  1. token_efficiency  — is the agent wasting tokens?\n"
        "  2. cache_performance — is the prompt/KV cache being used?\n"
        "  3. stability         — is the agent stuck or risky?\n"
        "\n"
        "Respond with a JSON object containing three keys "
        "(token_efficiency / cache_performance / stability). Each "
        "value is an object with: 'interpretation' (one sentence "
        "explaining what's happening — operator-friendly), and "
        "'next_action' (concrete next step, or null). Also include a "
        "top-level 'summary' field with a one-sentence cross-axis "
        "synthesis. Do NOT change scores or severities — those stay "
        "as the heuristic assigned them.\n"
        f"{knowledge_block}"
        "\n"
        "Signals:\n"
        f"  window: {s.n_total} calls over {s.window_seconds // 3600}h\n"
        f"  decisions: {s.n_allow} ALLOW / {s.n_block} BLOCK / "
        f"{s.n_approval} REQUIRE_APPROVAL\n"
        f"  total_cost_usd: ${s.total_cost_usd:.4f}\n"
        f"  top_cost_tool: {s.top_cost_tool} "
        f"(${s.top_cost_tool_total:.4f})\n"
        f"  repeat_call_ratio: {s.repeat_call_ratio:.2%}\n"
        f"  avg_tokens_per_call: {s.avg_tokens_per_call:,}\n"
        f"  est_cache_hit_rate: {s.estimated_cache_hit_rate:.2%}\n"
        f"  redundant_reads: {s.redundant_read_count}\n"
        f"  prefix_instability: {s.prefix_instability_count}\n"
        f"  loop_events: {s.loop_detected_count}\n"
        f"  dangerous_patterns: {s.dangerous_pattern_count}\n"
        f"  sensitive_paths: {s.sensitive_path_count}\n"
        f"  rule_matches: {s.rule_violation_count}\n"
        "\n"
        "Heuristic baseline (refine the prose, keep scores):\n"
        f"  token_efficiency  score={baseline.token_efficiency.score:.2f} "
        f"sev={baseline.token_efficiency.severity}\n"
        f"  cache_performance score={baseline.cache_performance.score:.2f} "
        f"sev={baseline.cache_performance.severity}\n"
        f"  stability         score={baseline.stability.score:.2f} "
        f"sev={baseline.stability.severity}\n"
        f"  overall_priority: {baseline.overall_priority}\n"
        "\n"
        "JSON response:"
    )


_JSON_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE,
)


def _extract_json_blob(text: str) -> str | None:
    """Same defensive extraction as ActionAdvice sLLM brain — fence,
    nested braces, prose-wrapped JSON all accepted."""
    if not text:
        return None
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _refine_axis(
    parsed: Any,
    key: str,
    baseline: AxisAssessment,
) -> tuple[AxisAssessment, bool]:
    """Merge a parsed `{axis: {interpretation, next_action}}` dict
    into the baseline assessment. Returns ``(refined, changed)``.
    Scores + severity are NEVER touched — sLLM is for prose only."""
    if not isinstance(parsed, dict):
        return baseline, False
    payload = parsed.get(key)
    if not isinstance(payload, dict):
        return baseline, False
    new_interp = payload.get("interpretation")
    new_action = payload.get("next_action")
    interp_str: str | None = None
    action_str: str | None = None
    if isinstance(new_interp, str) and new_interp.strip():
        interp_str = new_interp.strip()[:400]
    if isinstance(new_action, str) and new_action.strip() \
            and new_action.strip().lower() != "null":
        action_str = new_action.strip()[:300]
    changed = (
        (interp_str is not None and interp_str != baseline.interpretation)
        or (action_str is not None and action_str != baseline.next_action)
    )
    if not changed:
        return baseline, False
    return replace(
        baseline,
        interpretation=interp_str or baseline.interpretation,
        next_action=action_str if action_str is not None else baseline.next_action,
    ), True


def _default_llm_call(prompt: str) -> str | None:
    """Re-use the same dispatcher as the v0.5.9 ActionAdvice sLLM
    brain so operators don't have to configure two provider knobs."""
    try:
        from aegis.judge.action_advice_sllm import _default_llm_call as _f
    except ImportError:
        return None
    return _f(prompt)


def assess_via_sllm(
    s: AxisSignals,
    *,
    llm_call: Callable[[str], str | None] | None = None,
    knowledge_context: str | None = None,
) -> TripleAxisAdvice:
    """Heuristic baseline + sLLM prose refinement per axis. Falls
    back to pure heuristic on any LLM / parse failure.

    v0.5.16: ``knowledge_context`` is the agent's wiki block from
    :func:`aegis.knowledge.knowledge_context_for_advisor` — when
    supplied, the sLLM sees the agent's typical behaviour profile
    as background context, which produces interpretations that
    distinguish "this agent is acting unusually" from "this is
    business as usual for this agent"."""
    baseline = assess_via_heuristic(s)
    if s.n_total == 0:
        return baseline
    caller = llm_call if llm_call is not None else _default_llm_call
    try:
        response = caller(
            _build_sllm_prompt(s, baseline, knowledge_context=knowledge_context),
        )
    except Exception:  # noqa: BLE001 — advisor never raises
        return baseline
    if not response:
        return baseline
    blob = _extract_json_blob(response)
    if not blob:
        return baseline
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return baseline
    if not isinstance(parsed, dict):
        return baseline

    t, t_changed = _refine_axis(parsed, "token_efficiency",
                                baseline.token_efficiency)
    c, c_changed = _refine_axis(parsed, "cache_performance",
                                baseline.cache_performance)
    st, st_changed = _refine_axis(parsed, "stability",
                                  baseline.stability)
    new_summary = parsed.get("summary")
    summary = (
        new_summary.strip()[:400]
        if isinstance(new_summary, str) and new_summary.strip()
        else baseline.summary
    )
    if not (t_changed or c_changed or st_changed
            or summary != baseline.summary):
        return baseline
    return replace(
        baseline,
        token_efficiency=t,
        cache_performance=c,
        stability=st,
        summary=summary,
        advisor_kind="sllm",
        produced_at_ns=time.time_ns(),
    )


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────


def assess_triple_axis(
    records: Iterable[ContextMemoryRecord],
    *,
    window_seconds: int = 7 * 86400,
    prefer_sllm: bool | None = None,
    llm_call: Callable[[str], str | None] | None = None,
    aid: str | None = None,
    use_knowledge: bool | None = None,
) -> TripleAxisAdvice:
    """Top-level entry — read a ContextMemory window and produce a
    triple-axis assessment.

    ``prefer_sllm`` selection rules (in order):
      1. Explicit kwarg wins.
      2. Otherwise, ``AEGIS_TRIPLE_AXIS_PROVIDER=sllm`` env →
         sLLM path; anything else → heuristic.
      3. Default heuristic.

    v0.5.16: ``aid`` + ``use_knowledge`` opt the advisor into
    consuming the ContextMemory knowledge wiki built by
    ``aegis knowledge build``. Selection rules:

      1. Explicit ``use_knowledge=True`` kwarg wins.
      2. Otherwise, ``AEGIS_ADVISOR_USE_KNOWLEDGE=1`` env opts in.
      3. Default off (preserves v0.5.15 behaviour).

    When opted in AND ``aid`` is provided, the wiki entries for
    that agent (plus its top cross-referenced tools and patterns)
    are spliced into the sLLM prompt as background context. The
    knowledge lookup short-circuits silently if no wiki has been
    built, so this is safe to enable globally — the advisor
    simply falls back to the no-context prompt when no knowledge
    is available.
    """
    signals = extract_axis_signals(records, window_seconds=window_seconds)
    if prefer_sllm is None:
        env = os.environ.get(
            "AEGIS_TRIPLE_AXIS_PROVIDER", "",
        ).lower().strip()
        prefer_sllm = env == "sllm"
    if not prefer_sllm:
        return assess_via_heuristic(signals)

    if use_knowledge is None:
        try:
            from aegis.knowledge.advisor import advisor_knowledge_enabled
            use_knowledge = advisor_knowledge_enabled()
        except ImportError:
            use_knowledge = False
    knowledge_context: str | None = None
    if use_knowledge and aid:
        try:
            from aegis.knowledge.advisor import knowledge_context_for_advisor
            knowledge_context = knowledge_context_for_advisor(aid)
        except ImportError:
            knowledge_context = None

    return assess_via_sllm(
        signals,
        llm_call=llm_call,
        knowledge_context=knowledge_context,
    )


# ──────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────


_AXIS_LABELS: Final[dict[Axis, str]] = {
    "token_efficiency": "💰 Token efficiency",
    "cache_performance": "🧊 Cache performance",
    "stability": "🛡️  Stability",
}

_SEV_GLYPHS: Final[dict[Severity, str]] = {
    "ok": "🟢 ok",
    "warn": "🟡 warn",
    "alert": "🔴 alert",
}


def render_triple_axis(advice: TripleAxisAdvice) -> str:
    """Plain-text rendering for CLI / log output."""
    win = advice.window_seconds
    if win >= 86400:
        win_label = f"{win // 86400}d"
    elif win >= 3600:
        win_label = f"{win // 3600}h"
    else:
        win_label = f"{win}s"

    lines = [
        f"Triple-axis assessment ({advice.advisor_kind})",
        f"  records: {advice.n_records:,}    window: {win_label}",
        f"  overall priority: {_AXIS_LABELS[advice.overall_priority]}",
        f"  summary: {advice.summary}",
        "",
    ]
    for axis_obj in (
        advice.token_efficiency,
        advice.cache_performance,
        advice.stability,
    ):
        lines.append(
            f"  {_AXIS_LABELS[axis_obj.axis]:<24}  "
            f"score {axis_obj.score:.2f}  {_SEV_GLYPHS[axis_obj.severity]}"
        )
        lines.append(f"    interpretation: {axis_obj.interpretation}")
        if axis_obj.next_action:
            lines.append(f"    next_action:    {axis_obj.next_action}")
        if axis_obj.cited_signals:
            lines.append(
                f"    cited signals:  {', '.join(axis_obj.cited_signals)}"
            )
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "Axis",
    "AxisAssessment",
    "AxisSignals",
    "AdvisorKind",
    "Severity",
    "TripleAxisAdvice",
    "assess_triple_axis",
    "assess_via_heuristic",
    "assess_via_sllm",
    "extract_axis_signals",
    "render_triple_axis",
]
