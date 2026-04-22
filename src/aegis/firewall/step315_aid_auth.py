"""Step 315 — AID-region authorization (patent §5B M14).

Inserted between step 310 (argument inspection) and step 320 (blast
radius). Consults a per-AID authorization table that maps each AID to
its allowed_tools and allowed_paths. A mismatch is recorded in the
circuit breaker; if the AID has been quarantined, ALL subsequent
calls from that AID are immediately blocked regardless of the
proposed tool's safety.

T2 software emulation. T3 has the same logic in the hardware tag
comparator at the memory controller (¶[0063K]).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from aegis.config import settings
from aegis.firewall.circuit_breaker import CircuitBreaker
from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

# Module-level singleton; create_app() may swap it for tests.
_breaker = CircuitBreaker()


def get_circuit_breaker() -> CircuitBreaker:
    return _breaker


def set_circuit_breaker(cb: CircuitBreaker) -> None:
    global _breaker
    _breaker = cb


@lru_cache(maxsize=4)
def _load_aid_policy(policy_dir_str: str) -> dict[str, Any]:
    path = Path(policy_dir_str) / "aid_region.json"
    if not path.exists():
        # Default: permissive (mirrors current production-MVP behavior).
        return {
            "default_policy": {
                "allowed_tools": [],          # empty → allow anything
                "allowed_paths": [],
                "max_violations": 1_000_000,  # effectively no breaker
            },
            "aids": {},
        }
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def reset_policy_cache() -> None:
    _load_aid_policy.cache_clear()


def _aid_key(inp: ATVInput) -> str:
    role = (inp.role_id or "default-role")
    return f"{inp.header.tenant_id}:{role}"


def _path_from_args(args_json: str) -> str | None:
    """Extract a 'path' field from common tool args shapes."""
    if not args_json:
        return None
    m = re.search(r'"path"\s*:\s*"([^"]+)"', args_json)
    return m.group(1) if m else None


def run(atv: np.ndarray, inp: ATVInput, ctx: FirewallContext) -> StepResult:
    policy = _load_aid_policy(settings.aegis_policy_dir)
    aid_key = _aid_key(inp)
    rule = policy["aids"].get(aid_key, policy["default_policy"])

    # Hard-block if this AID is currently quarantined (prior violations).
    if _breaker.is_quarantined(inp.header.aid):
        return StepResult(
            "BLOCK",
            f"AID {inp.header.aid} is quarantined — admin release required",
            f"step315: quarantined-aid (aid={inp.header.aid})",
        )

    allowed_tools: list[str] = rule.get("allowed_tools", [])
    allowed_paths: list[str] = rule.get("allowed_paths", [])
    max_violations: int = int(rule.get("max_violations", 5))

    # Check 1: tool must be in the allowed_tools whitelist (empty list → allow all).
    if allowed_tools and inp.tool_name not in allowed_tools:
        st = _breaker.record_violation(
            inp.header.aid, max_allowed=max_violations,
            reason=f"unauthorized_tool:{inp.tool_name}",
        )
        return StepResult(
            "BLOCK",
            f"AID {aid_key} not authorized for tool {inp.tool_name}; "
            f"violations={st.violations}/{max_violations}",
            f"step315: aid-tool-deny (tool={inp.tool_name}, viol={st.violations})",
        )

    # Check 2: if a 'path' arg is present, it must start with an allowed prefix.
    path = _path_from_args(inp.tool_args_json)
    if path is not None and allowed_paths and not any(
        path.startswith(p) for p in allowed_paths
    ):
        st = _breaker.record_violation(
            inp.header.aid, max_allowed=max_violations,
            reason=f"unauthorized_path:{path}",
        )
        return StepResult(
            "BLOCK",
            f"AID {aid_key} not authorized for path {path}; "
            f"violations={st.violations}/{max_violations}",
            f"step315: aid-path-deny (path={path}, viol={st.violations})",
        )

    return StepResult(None, "", f"step315: ok (aid={aid_key})")
