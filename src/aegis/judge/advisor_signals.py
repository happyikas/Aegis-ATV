"""Signal extraction + narrative rendering for the multi-domain advisor
(PR-ψ-multi-domain, Phase C tier 3 enrichment).

The Tier-3 advisor (PR-ζ-head) consumes a 4-layer narrative built from
:mod:`aegis.atv.temporal` + burn-in modules. PR-ψ-multi-domain adds three
domain-specific sections to that narrative — COST METRICS, KV CACHE
METRICS, SECURITY SIGNALS — and a structured signal-dict shape that the
heuristic composer maps to :class:`AdvisorRecommendation` instances.

Two halves
----------

* :func:`extract_*` — pure functions that pull a small dict of named
  metrics out of (``inp``, ``verdict``, ``temporal_ctx``). No heavy
  computation; everything is already in memory at PreToolUse time.
* :func:`render_*` — turn a dict into the human-readable section that
  goes into the sLLM user message.

Both halves are decoupled from the firewall types: callers pass
``Any``-typed objects, and we use ``getattr`` / ``.get`` so a missing
attribute degrades to "section omitted" rather than crashing.

Why split extract from render?
------------------------------

* The hook calls :func:`extract_*` and passes the resulting dicts
  through to :func:`compose_advice_sllm` (so the heuristic composer
  can map them to :class:`AdvisorRecommendation` deterministically).
* :mod:`aegis.judge.advisor` calls :func:`render_*` to produce the
  prompt text shown to Haiku.
* Tests can synthesise dicts directly without building a full ATV.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aegis.atv.temporal import TemporalContext


# ──────────────────────────────────────────────────────────────────────
# COST signals
# ──────────────────────────────────────────────────────────────────────


_STEP335_NUM_RE = re.compile(r"cum=([\d.]+)")
_STEP335_PROJ_RE = re.compile(r"proj=([\d.]+)")
_STEP335_LIMIT_RE = re.compile(r"limit=([\d.]+)")
_M12_RATIO_RE = re.compile(r"=([\d.]+) >")


def extract_cost_signals(
    *,
    inp: Any,
    verdict: Any,
) -> dict[str, Any]:
    """Pull cost-domain signals from the firewall's already-computed
    state. Returns ``{}`` when nothing is extractable (so callers can
    treat empty as "no COST METRICS section").

    Source order:

    * ``step_traces["aegis.firewall.step335_cost.run"]`` — string with
      ``cum=…`` / ``proj=…`` / ``limit=…`` / ``warn`` markers.
    * ``step_traces["aegis.cost.escalation"]`` — M12 cost-divergence,
      ``"M12: <metric>=<obs> > threshold <thresh>"`` shape.
    * ``inp.cost_estimate`` — last-resort source for cumulative_dollars.
    """
    out: dict[str, Any] = {}
    traces = _get_traces(verdict)

    s335 = str(traces.get("aegis.firewall.step335_cost.run", ""))
    if s335:
        cum = _try_float(_STEP335_NUM_RE.search(s335))
        proj = _try_float(_STEP335_PROJ_RE.search(s335))
        limit = _try_float(_STEP335_LIMIT_RE.search(s335))
        if cum is not None:
            out["cumulative_dollars"] = cum
        if proj is not None:
            out["projected_session_cost"] = proj
        if limit is not None:
            out["budget_limit"] = limit
            if proj is not None and limit > 0:
                out["budget_used_ratio"] = proj / limit
        if "warn" in s335.lower():
            out["budget_warn_flag"] = True

    esc = str(traces.get("aegis.cost.escalation", ""))
    if esc:
        ratio = _try_float(_M12_RATIO_RE.search(esc))
        if ratio is not None:
            out["hw_vs_sw_divergence_ratio"] = ratio
        out["m12_escalation_trace"] = esc[:120]

    if "cumulative_dollars" not in out:
        cost_est = getattr(inp, "cost_estimate", None)
        cum = getattr(cost_est, "cumulative_dollars", None)
        if isinstance(cum, (int, float)) and cum > 0:
            out["cumulative_dollars"] = float(cum)

    return out


def render_cost_signals(d: dict[str, Any]) -> str:
    """Render the COST METRICS section. Empty dict → empty string so
    the caller can omit the section header."""
    if not d:
        return ""
    lines = ["COST METRICS"]
    if "cumulative_dollars" in d:
        lines.append(f"  cumulative_dollars:        ${d['cumulative_dollars']:.4f}")
    if "projected_session_cost" in d:
        proj = d["projected_session_cost"]
        ratio = d.get("budget_used_ratio")
        if isinstance(ratio, (int, float)):
            lines.append(
                f"  projected_session_cost:    ${proj:.4f}  "
                f"({ratio*100:.1f}% of budget)"
            )
        else:
            lines.append(f"  projected_session_cost:    ${proj:.4f}")
    if "budget_limit" in d:
        lines.append(f"  budget_limit:              ${d['budget_limit']:.2f}")
    if d.get("budget_warn_flag"):
        lines.append("  budget_warn_flag:          true (approaching limit)")
    if "hw_vs_sw_divergence_ratio" in d:
        ratio = d["hw_vs_sw_divergence_ratio"]
        lines.append(
            f"  hw_vs_sw_divergence_ratio: {ratio:.2f}× "
            f"({'normal' if ratio < 2.0 else 'ESCALATED'})"
        )
    if "m12_escalation_trace" in d:
        lines.append(f"  m12_trace:                 {d['m12_escalation_trace']}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# KV CACHE signals
# ──────────────────────────────────────────────────────────────────────


def extract_cache_signals(
    *,
    temporal_ctx: TemporalContext | None,
) -> dict[str, Any]:
    """Pull KV-cache signals from the temporal window. All values are
    derived from per-turn ``cache_*_tokens`` already on the
    :class:`ATVSnapshot` ring buffer."""
    if temporal_ctx is None or not temporal_ctx.history:
        return {}
    out: dict[str, Any] = {}

    rates = list(temporal_ctx.cache_hit_rate_trajectory)
    if rates:
        out["cache_hit_rate_recent"] = rates[-1]
        out["cache_hit_rate_window_mean"] = sum(rates) / len(rates)
        out["cache_hit_rate_max_drop_pp"] = (
            float(temporal_ctx.cache_hit_rate_max_drop_pp)
        )

    creation_total = sum(
        s.cache_creation_tokens for s in temporal_ctx.history
    )
    read_total = sum(
        s.cache_read_tokens for s in temporal_ctx.history
    )
    if creation_total or read_total:
        out["cache_creation_tokens_window"] = int(creation_total)
        out["cache_read_tokens_window"] = int(read_total)
        if creation_total > 0:
            out["cache_creation_to_read_ratio"] = (
                read_total / max(creation_total, 1)
            )

    # Prefix instability heuristic — count turns where cache_creation
    # > cache_read (i.e. prefix re-keyed). 3+ in window = "unstable".
    re_keys = sum(
        1
        for s in temporal_ctx.history
        if s.cache_creation_tokens > s.cache_read_tokens
        and s.cache_creation_tokens > 0
    )
    out["prefix_re_keys_in_window"] = int(re_keys)
    out["prefix_stability"] = (
        "unstable" if re_keys >= 3 else "stable"
    )
    return out


def render_cache_signals(d: dict[str, Any]) -> str:
    if not d:
        return ""
    lines = ["KV CACHE METRICS"]
    if "cache_hit_rate_recent" in d:
        lines.append(
            f"  cache_hit_rate_recent:     {d['cache_hit_rate_recent']:.2f}"
        )
    if "cache_hit_rate_window_mean" in d:
        lines.append(
            f"  cache_hit_rate_window_mean:{d['cache_hit_rate_window_mean']:.2f}"
        )
    if "cache_hit_rate_max_drop_pp" in d:
        drop = d["cache_hit_rate_max_drop_pp"]
        flag = " ← significant drop" if drop > 30 else ""
        lines.append(
            f"  cache_hit_rate_max_drop_pp:{drop:.0f}pp{flag}"
        )
    if "cache_creation_tokens_window" in d:
        lines.append(
            f"  cache_creation_tokens:     "
            f"{d['cache_creation_tokens_window']}"
        )
    if "cache_read_tokens_window" in d:
        lines.append(
            f"  cache_read_tokens:         {d['cache_read_tokens_window']}"
        )
    if "prefix_stability" in d:
        re_keys = d.get("prefix_re_keys_in_window", 0)
        lines.append(
            f"  prefix_stability:          {d['prefix_stability']}  "
            f"({re_keys} prefix re-keys / window)"
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# SECURITY signals
# ──────────────────────────────────────────────────────────────────────


_DESTRUCTIVE_RULES: frozenset[str] = frozenset({
    "rule:git_destructive", "rule:sandbox_escape", "rule:payment_overflow",
    "rule:sql_unbounded", "rule:rm_rf", "rule:fs_destructive",
    "rule:backup_path_destructive", "rule:credential_exfil",
})


def extract_security_signals(
    *,
    inp: Any,
    verdict: Any,
    explain_block: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pull security-domain signals.

    Sources:

    * ``verdict.reason`` — usually carries the rule name on BLOCK.
    * ``step_traces["aegis.firewall.step320_blast.run"]`` — blast radius.
    * ``step_traces["aegis.firewall.step310_destructive.run"]`` /
      ``step311_donor_rules.run`` — pattern matches.
    * ``explain_block["m13_top"]`` — whether security-domain ATV
      subfields are top contributors.
    """
    out: dict[str, Any] = {}
    traces = _get_traces(verdict)

    decision = getattr(verdict, "decision", "ALLOW")
    reason = str(getattr(verdict, "reason", "") or "")
    out["verdict_decision"] = decision

    for rule in _DESTRUCTIVE_RULES:
        if rule in reason:
            out["destructive_path_match"] = True
            out["policy_rule"] = rule
            break

    s320 = str(traces.get("aegis.firewall.step320_blast.run", ""))
    if "high" in s320.lower():
        out["blast_radius"] = "high"
    elif "medium" in s320.lower():
        out["blast_radius"] = "medium"
    elif "low" in s320.lower():
        out["blast_radius"] = "low"

    s310 = str(traces.get("aegis.firewall.step310_destructive.run", ""))
    s311 = str(traces.get("aegis.firewall.step311_donor_rules.run", ""))
    s312 = str(traces.get("aegis.firewall.step312_normalize.run", ""))
    matched: list[str] = []
    for label, txt in (("310", s310), ("311", s311), ("312", s312)):
        low = txt.lower()
        if "block" in low or "match" in low or "rule:" in low:
            matched.append(f"step{label}")
    if matched:
        out["pattern_match_steps"] = matched

    tool_args = str(getattr(inp, "tool_args_json", "") or "")
    if any(p in tool_args.lower() for p in ("/backup/", "/etc/", "credentials", ".pem", ".key")):
        out["sensitive_path_in_args"] = True

    if explain_block:
        m13_top = explain_block.get("m13_top") or []
        sec_subs = {
            "tool_arg_inspection", "memory_provenance",
            "action_blast_radius", "qom_scores",
        }
        sec_top = [
            entry for entry in m13_top
            if isinstance(entry, dict)
            and entry.get("subfield") in sec_subs
        ]
        if sec_top:
            out["m13_security_top"] = [
                {
                    "subfield": e["subfield"],
                    "score": float(e.get("score", 0.0)),
                }
                for e in sec_top
            ]

    return out


def render_security_signals(d: dict[str, Any]) -> str:
    if not d:
        return ""
    lines = ["SECURITY SIGNALS"]
    if "verdict_decision" in d:
        lines.append(f"  verdict_decision:          {d['verdict_decision']}")
    if d.get("destructive_path_match"):
        rule = d.get("policy_rule", "(unknown)")
        lines.append(f"  destructive_path_match:    yes ({rule})")
    if "blast_radius" in d:
        lines.append(f"  blast_radius:              {d['blast_radius']}")
    if "pattern_match_steps" in d:
        lines.append(
            f"  pattern_match_steps:       "
            f"{', '.join(d['pattern_match_steps'])}"
        )
    if d.get("sensitive_path_in_args"):
        lines.append("  sensitive_path_in_args:    yes")
    if "m13_security_top" in d:
        names = [str(e.get("subfield", "")) for e in d["m13_security_top"]]
        lines.append(
            f"  m13_security_top:          {', '.join(names)}"
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _get_traces(verdict: Any) -> dict[str, Any]:
    raw = getattr(verdict, "step_traces", None) or {}
    if isinstance(raw, dict):
        return raw
    return {}


def _try_float(match: re.Match[str] | None) -> float | None:
    if match is None:
        return None
    try:
        return float(match.group(1))
    except (ValueError, IndexError):
        return None


__all__ = [
    "extract_cache_signals",
    "extract_cost_signals",
    "extract_security_signals",
    "render_cache_signals",
    "render_cost_signals",
    "render_security_signals",
]
