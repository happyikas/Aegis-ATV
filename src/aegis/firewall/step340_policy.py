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
        f"Cost estimate: bytes={inp.cost_estimate.exp_bytes_write}, "
        f"$={inp.cost_estimate.exp_dollars}"
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

    judge = get_judge()
    jv = judge.evaluate(atv_summary_for_llm(inp))
    if jv.decision == "ALLOW":
        return StepResult(
            None,
            "",
            f"step340: sLLM allow (conf={jv.confidence:.2f})",
        )
    if jv.decision == "BLOCK":
        return StepResult(
            "BLOCK",
            jv.reason or "sLLM judge: block",
            f"step340: sLLM block (conf={jv.confidence:.2f})",
        )
    return StepResult(
        "REQUIRE_APPROVAL",
        jv.reason or "sLLM judge: approval",
        f"step340: sLLM approval (conf={jv.confidence:.2f})",
    )
