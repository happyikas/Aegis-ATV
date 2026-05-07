"""Tests for the patent-aligned identifier layer added in PR #100.

Two contracts:

1. **Back-compat** — legacy callers that fill only v1 fields
   (trace_id / span_id / aid / node_id / pod_id) get sensible
   derived values for the new fields. Existing code paths keep
   working.

2. **Explicit override** — new callers can fill the patent-aligned
   fields directly (agent_id, agent_instance_id, session_id,
   parent_atv_hash, step_seq_no, runtime_context_id) and the
   validator does NOT clobber their values.
"""
from __future__ import annotations

from aegis.schema import ATVHeader

# ── back-compat: legacy callers ──────────────────────────────────────


class TestLegacyBackCompat:
    def _legacy_only(self) -> ATVHeader:
        return ATVHeader(
            trace_id="trace-abc", span_id="span-xyz",
            tenant_id="solo-free-local", aid="aid-uuid-1",
            timestamp_ns=1714857655_000_000_000,
            node_id="mac-mini-01", pod_id="pod-7",
        )

    def test_aid_drives_agent_instance_id(self) -> None:
        h = self._legacy_only()
        assert h.agent_instance_id == "aid-uuid-1"

    def test_aid_also_drives_logical_agent_id_in_legacy_mode(self) -> None:
        """When the caller didn't separate logical vs instance, the
        legacy AID stands in for both (degenerate Solo Free case)."""
        h = self._legacy_only()
        assert h.agent_id == "aid-uuid-1"

    def test_trace_id_drives_session_id(self) -> None:
        h = self._legacy_only()
        assert h.session_id == "trace-abc"

    def test_span_id_drives_action_txn_id(self) -> None:
        h = self._legacy_only()
        assert h.action_txn_id == "span-xyz"

    def test_node_pod_consolidate_into_runtime_context(self) -> None:
        h = self._legacy_only()
        assert h.runtime_context_id == "mac-mini-01:pod-7"

    def test_node_alone_drives_runtime_context(self) -> None:
        h = ATVHeader(
            trace_id="t", span_id="s", tenant_id="t1", aid="a",
            timestamp_ns=1, node_id="node-only",
        )
        assert h.runtime_context_id == "node-only"
        assert h.deployment_id == "node-only"

    def test_pod_alone_drives_runtime_context(self) -> None:
        h = ATVHeader(
            trace_id="t", span_id="s", tenant_id="t1", aid="a",
            timestamp_ns=1, pod_id="pod-only",
        )
        assert h.runtime_context_id == "pod-only"

    def test_no_runtime_metadata_leaves_runtime_context_none(self) -> None:
        h = ATVHeader(
            trace_id="t", span_id="s", tenant_id="t1", aid="a",
            timestamp_ns=1,
        )
        assert h.runtime_context_id is None
        assert h.deployment_id is None

    def test_step_seq_no_defaults_to_zero(self) -> None:
        h = self._legacy_only()
        assert h.step_seq_no == 0

    def test_parent_atv_hash_defaults_to_none(self) -> None:
        h = self._legacy_only()
        assert h.parent_atv_hash is None


# ── explicit override: new callers fill patent fields ────────────────


class TestExplicitOverride:
    def test_explicit_agent_id_separates_from_instance(self) -> None:
        """A long-running 'MedRAG-CKD' deployment has many ephemeral
        agent_instance_ids (one per process) but a single stable
        agent_id ('MedRAG-CKD'). The validator must respect that."""
        h = ATVHeader(
            trace_id="t", span_id="s", tenant_id="hallym-medical",
            aid="instance-uuid-2026-w18",
            timestamp_ns=1,
            agent_id="MedRAG-CKD",
            agent_instance_id="instance-uuid-2026-w18",
        )
        assert h.agent_id == "MedRAG-CKD"
        assert h.agent_instance_id == "instance-uuid-2026-w18"
        assert h.aid == "instance-uuid-2026-w18"  # legacy field unchanged

    def test_explicit_session_id_overrides_trace_alias(self) -> None:
        h = ATVHeader(
            trace_id="otel-trace-xxx", span_id="otel-span-yyy",
            tenant_id="t", aid="a", timestamp_ns=1,
            session_id="hallym-session-2026-Q2-128",
        )
        assert h.session_id == "hallym-session-2026-Q2-128"
        # trace_id is preserved for OTel propagation
        assert h.trace_id == "otel-trace-xxx"

    def test_explicit_action_txn_overrides_span_alias(self) -> None:
        h = ATVHeader(
            trace_id="t", span_id="otel-span-yyy",
            tenant_id="t", aid="a", timestamp_ns=1,
            action_txn_id="txn-bash-rm-rf-001",
        )
        assert h.action_txn_id == "txn-bash-rm-rf-001"
        assert h.span_id == "otel-span-yyy"

    def test_step_seq_no_explicit(self) -> None:
        h = ATVHeader(
            trace_id="t", span_id="s", tenant_id="t", aid="a",
            timestamp_ns=1, step_seq_no=42,
        )
        assert h.step_seq_no == 42

    def test_parent_atv_hash_for_call_tree(self) -> None:
        """A sub-agent's session_init record points back to the
        invocation record in the parent agent's chain."""
        h = ATVHeader(
            trace_id="t", span_id="s", tenant_id="t", aid="a",
            timestamp_ns=1,
            parent_atv_hash="sha3:abc...def",
        )
        assert h.parent_atv_hash == "sha3:abc...def"

    def test_runtime_context_explicit_overrides_node_pod(self) -> None:
        """Explicit TEE quote takes precedence over node:pod
        consolidation."""
        h = ATVHeader(
            trace_id="t", span_id="s", tenant_id="t", aid="a",
            timestamp_ns=1,
            node_id="node-a", pod_id="pod-1",
            runtime_context_id="tee-quote:abc123def",
        )
        assert h.runtime_context_id == "tee-quote:abc123def"

    def test_full_patent_aligned_payload(self) -> None:
        """The kind of header a Tier-2 multi-tenant deployment would
        actually emit: every patent-aligned field set explicitly."""
        h = ATVHeader(
            schema_version="ATV-2080-v1",
            trace_id="otel-trace-xxx", span_id="otel-span-yyy",
            tenant_id="hallym-medical",
            aid="instance-uuid-w18",
            timestamp_ns=1714857655_000_000_000,
            tier_profile="T3",
            agent_id="MedRAG-CKD",
            agent_instance_id="instance-uuid-w18",
            session_id="sess-2026-Q2-128",
            runtime_context_id="tee-quote:abc123",
            step_seq_no=5,
            action_txn_id="txn-001",
            parent_atv_hash="sha3:parent...",
            deployment_id="prod-cluster-seoul",
            policy_id="policy-2026-04-15",
            attestation_key_id="hallym-key-001",
        )
        # Every patent-aligned field came through unchanged
        assert h.agent_id == "MedRAG-CKD"
        assert h.agent_instance_id == "instance-uuid-w18"
        assert h.session_id == "sess-2026-Q2-128"
        assert h.runtime_context_id == "tee-quote:abc123"
        assert h.step_seq_no == 5
        assert h.action_txn_id == "txn-001"
        assert h.parent_atv_hash == "sha3:parent..."
        assert h.deployment_id == "prod-cluster-seoul"
        assert h.policy_id == "policy-2026-04-15"
        assert h.attestation_key_id == "hallym-key-001"


# ── invariants ────────────────────────────────────────────────────────


class TestInvariants:
    def test_legacy_v1_caller_round_trips_through_dict(self) -> None:
        """A v1-only header serialised + parsed round-trips both
        legacy fields and derived patent-aligned fields."""
        original = ATVHeader(
            trace_id="t", span_id="s", tenant_id="t1", aid="a1",
            timestamp_ns=1, node_id="node-a", pod_id="pod-1",
        )
        d = original.model_dump()
        # Both layers in the dump
        assert d["aid"] == "a1"
        assert d["agent_instance_id"] == "a1"
        assert d["session_id"] == "t"
        # Re-parse and re-derive
        roundtrip = ATVHeader.model_validate(d)
        assert roundtrip.agent_instance_id == original.agent_instance_id
        assert roundtrip.session_id == original.session_id

    def test_back_compat_invariant_for_existing_audit_lines(self) -> None:
        """An audit JSONL line written before PR #100 (no patent-
        aligned fields) loads cleanly under the v2 schema."""
        legacy_audit_dict = {
            "trace_id": "old-trace",
            "span_id": "old-span",
            "tenant_id": "claude-code-local",
            "aid": "old-aid",
            "timestamp_ns": 1700000000_000_000_000,
            "schema_version": "ATV-2080-v1",
        }
        h = ATVHeader.model_validate(legacy_audit_dict)
        assert h.aid == "old-aid"
        assert h.agent_instance_id == "old-aid"  # derived
        assert h.session_id == "old-trace"       # derived
        assert h.step_seq_no == 0                # default

    def test_explicit_layer_does_not_clobber_legacy(self) -> None:
        """Filling patent-aligned fields must not modify legacy ones."""
        h = ATVHeader(
            trace_id="otel-x", span_id="otel-y",
            tenant_id="t", aid="legacy-aid",
            timestamp_ns=1,
            agent_id="MedRAG-CKD",
            agent_instance_id="uuid-w18",
            session_id="explicit-sess",
        )
        assert h.aid == "legacy-aid"        # legacy untouched
        assert h.trace_id == "otel-x"       # legacy untouched
        assert h.span_id == "otel-y"        # legacy untouched
