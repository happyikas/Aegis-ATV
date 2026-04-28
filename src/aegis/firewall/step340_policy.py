"""Step 340 — Policy match + sLLM judge fallback (PLAN 6.4)."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from aegis.config import settings
from aegis.firewall.core import FirewallContext, StepResult
from aegis.judge import get_judge
from aegis.schema import ATVInput


@lru_cache(maxsize=8)
def _load_policies(path_str: str) -> dict[str, list[dict[str, Any]]]:
    path = Path(path_str) / "default.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {"deny": list(raw.get("deny", [])), "allow": list(raw.get("allow", []))}


def load_policies() -> dict[str, list[dict[str, Any]]]:
    return _load_policies(settings.aegis_policy_dir)


def reset_policy_cache() -> None:
    _load_policies.cache_clear()


def match_rule(rule: dict[str, Any], inp: ATVInput) -> bool:
    if "tool_name" in rule and rule["tool_name"] != inp.tool_name:
        return False
    if "tenant_id" in rule and rule["tenant_id"] != inp.header.tenant_id:
        return False
    return not ("arg_pattern" in rule and not re.search(rule["arg_pattern"], inp.tool_args_json))


def atv_summary_for_llm(inp: ATVInput) -> str:
    return (
        f"Tool: {inp.tool_name}\n"
        f"Args: {inp.tool_args_json[:500]}\n"
        f"Tenant: {inp.header.tenant_id}\n"
        f"Plan: {inp.plan_text[:300]}\n"
        f"Safety scores: {inp.safety_flags}\n"
        f"Cost estimate: tokens_in={inp.cost_estimate.input_token_count:.0f}, "
        f"$={inp.cost_estimate.cumulative_dollars:.4f}, "
        f"forecast=${inp.cost_estimate.forecasted_cost_to_completion:.4f}"
    )


def run(atv: np.ndarray, inp: ATVInput, ctx: FirewallContext) -> StepResult:
    rules = load_policies()

    for rule in rules["deny"]:
        if match_rule(rule, inp):
            return StepResult(
                "BLOCK",
                f"policy deny: {rule.get('name', '<unnamed>')}",
                f"step340: deny match {rule.get('name', '<unnamed>')}",
            )

    for rule in rules["allow"]:
        if match_rule(rule, inp):
            return StepResult(
                None,
                "",
                f"step340: allow match {rule.get('name', '<unnamed>')}",
            )

    # v2.1 Day-1 #1: skip the sLLM judge round-trip when step305 flagged
    # the call as safely fast-pathable. The earlier gates (310 dangerous
    # regex, 311 donor rules, 320 blast, 335 cost) have already cleared
    # this call, so the judge would almost always return ALLOW anyway —
    # we save the latency and the LLM token cost.
    if ctx.extras.get("safe_fast_path"):
        return StepResult(
            None,
            "",
            f"step340: skipped (safe_fast_path={ctx.extras.get('safe_match', '?')})",
        )

    judge = get_judge()
    # v2.5+: pass the live ATV vector + ATVInput so M13-style judges
    # (AttributionHead / Hybrid) can read the 30 named subfields directly.
    # Legacy judges (Dummy / Haiku) inherit Judge.evaluate_full's default
    # which falls back to ``evaluate(summary)`` — backward compatible.
    jv = judge.evaluate_full(atv_summary_for_llm(inp), atv=atv, inp=inp)

    # M13: surface top-3 attributed subfields in the trace so dashboards
    # and the Theater pipeline panel can render the attention head.
    top_attr = ""
    if jv.subfield_attribution:
        top = sorted(jv.subfield_attribution.items(), key=lambda kv: -kv[1])[:3]
        top_attr = " attr=[" + ", ".join(f"{k}:{v:.2f}" for k, v in top) + "]"
        ctx.extras["subfield_attribution"] = jv.subfield_attribution

    if jv.decision == "ALLOW":
        return StepResult(
            None,
            "",
            f"step340: sLLM allow (conf={jv.confidence:.2f}){top_attr}",
        )
    if jv.decision == "BLOCK":
        return StepResult(
            "BLOCK",
            jv.reason or "sLLM judge: block",
            f"step340: sLLM block (conf={jv.confidence:.2f}){top_attr}",
        )
    return StepResult(
        "REQUIRE_APPROVAL",
        jv.reason or "sLLM judge: approval",
        f"step340: sLLM approval (conf={jv.confidence:.2f}){top_attr}",
    )
