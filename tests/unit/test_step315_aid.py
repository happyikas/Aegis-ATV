"""Tests for step 315 — AID-region authorization + circuit breaker (M14)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from aegis.firewall.circuit_breaker import AidStatus, CircuitBreaker
from aegis.firewall.core import FirewallContext
from aegis.firewall.step315_aid_auth import (
    get_circuit_breaker,
    reset_policy_cache,
    run,
    set_circuit_breaker,
)
from aegis.schema import ATVHeader, ATVInput
from tests.unit._firewall_helpers import ZERO_ATV


def _inp(*, aid: str = "alice", role: str = "default-role",
         tenant: str = "demo-tenant", tool: str = "read_file",
         args: str = '{"path":"./data/x.txt"}') -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t", span_id="s", tenant_id=tenant, aid=aid,
            timestamp_ns=time.time_ns(),
        ),
        plan_text="p", tool_name=tool, tool_args_json=args,
        role_id=role,
    )


@pytest.fixture
def strict_policy(tmp_path: Path) -> Path:
    """Write a strict policy file and patch settings.aegis_policy_dir."""
    rules = {
        "default_policy": {
            "allowed_tools": ["read_file"],
            "allowed_paths": ["./data/"],
            "max_violations": 3,
        },
        "aids": {
            "demo-tenant:trusted-role": {
                "allowed_tools": ["read_file", "execute_shell"],
                "allowed_paths": ["./data/", "./bin/"],
                "max_violations": 5,
            },
        },
    }
    (tmp_path / "aid_region.json").write_text(json.dumps(rules))
    reset_policy_cache()
    cb = CircuitBreaker()
    set_circuit_breaker(cb)
    with patch("aegis.firewall.step315_aid_auth.settings") as s:
        s.aegis_policy_dir = str(tmp_path)
        yield tmp_path
    reset_policy_cache()


# ─────────────────────────────────────────────────────────────────────
# Tool whitelist enforcement
# ─────────────────────────────────────────────────────────────────────
class TestToolWhitelist:
    def test_default_role_allowed_tool_passes(self, strict_policy: Path) -> None:
        r = run(ZERO_ATV, _inp(tool="read_file"), FirewallContext())
        assert r.verdict is None

    def test_default_role_disallowed_tool_blocked(self, strict_policy: Path) -> None:
        r = run(ZERO_ATV, _inp(tool="execute_shell"), FirewallContext())
        assert r.verdict == "BLOCK"
        assert "not authorized for tool" in r.reason

    def test_special_role_grants_extra_tools(self, strict_policy: Path) -> None:
        r = run(ZERO_ATV, _inp(role="trusted-role", tool="execute_shell",
                                args='{"command":"ls"}'),
                FirewallContext())
        assert r.verdict is None


# ─────────────────────────────────────────────────────────────────────
# Path prefix enforcement
# ─────────────────────────────────────────────────────────────────────
class TestPathPrefix:
    def test_allowed_path_passes(self, strict_policy: Path) -> None:
        r = run(ZERO_ATV, _inp(args='{"path":"./data/report.txt"}'),
                FirewallContext())
        assert r.verdict is None

    def test_disallowed_path_blocked(self, strict_policy: Path) -> None:
        r = run(ZERO_ATV, _inp(args='{"path":"/etc/passwd"}'),
                FirewallContext())
        assert r.verdict == "BLOCK"
        assert "not authorized for path" in r.reason


# ─────────────────────────────────────────────────────────────────────
# Circuit breaker: violation counting + auto-quarantine + hard block
# ─────────────────────────────────────────────────────────────────────
class TestCircuitBreaker:
    def test_violations_accumulate_and_quarantine(self, strict_policy: Path) -> None:
        cb = get_circuit_breaker()
        # Default max_violations = 3 → 3 disallowed-tool calls quarantines.
        for _ in range(3):
            r = run(ZERO_ATV, _inp(aid="rogue", tool="execute_shell"),
                    FirewallContext())
            assert r.verdict == "BLOCK"
        assert cb.is_quarantined("rogue")

    def test_quarantined_aid_blocked_even_for_allowed_tool(
        self, strict_policy: Path
    ) -> None:
        cb = get_circuit_breaker()
        # Trip the breaker.
        for _ in range(3):
            run(ZERO_ATV, _inp(aid="rogue", tool="execute_shell"), FirewallContext())
        # Now even an otherwise-allowed call (read_file) is blocked.
        r = run(ZERO_ATV, _inp(aid="rogue", tool="read_file"), FirewallContext())
        assert r.verdict == "BLOCK"
        assert "quarantined" in r.reason
        assert cb.is_quarantined("rogue")

    def test_separate_aids_have_separate_counters(self, strict_policy: Path) -> None:
        cb = get_circuit_breaker()
        for _ in range(3):
            run(ZERO_ATV, _inp(aid="rogue", tool="execute_shell"), FirewallContext())
        # Different AID — clean slate.
        r = run(ZERO_ATV, _inp(aid="alice", tool="read_file"), FirewallContext())
        assert r.verdict is None
        assert cb.is_quarantined("rogue")
        assert not cb.is_quarantined("alice")

    def test_release_reactivates_aid(self, strict_policy: Path) -> None:
        cb = get_circuit_breaker()
        for _ in range(3):
            run(ZERO_ATV, _inp(aid="rogue", tool="execute_shell"), FirewallContext())
        assert cb.is_quarantined("rogue")
        cb.release("rogue", reason="ops investigation done")
        assert not cb.is_quarantined("rogue")
        # Allowed tool now works again.
        r = run(ZERO_ATV, _inp(aid="rogue", tool="read_file"), FirewallContext())
        assert r.verdict is None


# ─────────────────────────────────────────────────────────────────────
# Default permissive policy (no restriction when allowed_tools=[])
# ─────────────────────────────────────────────────────────────────────
class TestPermissiveDefault:
    def test_empty_allowed_tools_means_pass_anything(self, tmp_path: Path) -> None:
        rules = {"default_policy": {"allowed_tools": [], "allowed_paths": [],
                                    "max_violations": 5}, "aids": {}}
        (tmp_path / "aid_region.json").write_text(json.dumps(rules))
        reset_policy_cache()
        with patch("aegis.firewall.step315_aid_auth.settings") as s:
            s.aegis_policy_dir = str(tmp_path)
            r = run(ZERO_ATV, _inp(tool="transfer_funds",
                                    args='{"amount":1000}'), FirewallContext())
            assert r.verdict is None
        reset_policy_cache()


# ─────────────────────────────────────────────────────────────────────
# CircuitBreaker introspection
# ─────────────────────────────────────────────────────────────────────
class TestCircuitBreakerIntrospection:
    def test_get_returns_none_for_unknown_aid(self) -> None:
        cb = CircuitBreaker()
        assert cb.get("never-existed") is None

    def test_history_records_violations_and_quarantine(self) -> None:
        cb = CircuitBreaker()
        for _ in range(2):
            cb.record_violation("x", max_allowed=2, reason="bad-tool")
        st = cb.get("x")
        assert st is not None
        assert st.status == AidStatus.QUARANTINED
        kinds = [h["kind"] for h in st.history]
        assert kinds == ["violation", "violation", "quarantine"]
