"""Payload-mix tests."""

from __future__ import annotations

from collections import Counter

from aegis.soak.payloads import (
    PAYLOAD_MIX,
    PayloadKind,
    allow_payload,
    approval_payload,
    block_payload,
    payload_for,
)


def test_default_mix_distribution_matches_weights() -> None:
    """1000 samples → counts should approximate the 70/15/15 default."""
    counts: Counter[PayloadKind] = Counter()
    # Deterministic pseudo-random walk through [0,1)
    n = 1000
    for i in range(n):
        kind, _, _ = payload_for((i + 0.5) / n)
        counts[kind] += 1
    # Within ±2% of the configured weights.
    assert abs(counts[PayloadKind.ALLOW] / n - 0.70) < 0.02
    assert abs(counts[PayloadKind.APPROVAL] / n - 0.15) < 0.02
    assert abs(counts[PayloadKind.BLOCK] / n - 0.15) < 0.02


def test_each_kind_returns_expected_decision() -> None:
    # Force each path by passing a deterministic rng_value.
    # ALLOW lives in [0, 0.7); APPROVAL in [0.7, 0.85); BLOCK in [0.85, 1).
    kind_a, _, dec_a = payload_for(0.0)
    kind_b, _, dec_b = payload_for(0.75)
    kind_c, _, dec_c = payload_for(0.95)

    assert (kind_a, dec_a) == (PayloadKind.ALLOW, "ALLOW")
    assert (kind_b, dec_b) == (PayloadKind.APPROVAL, "REQUIRE_APPROVAL")
    assert (kind_c, dec_c) == (PayloadKind.BLOCK, "BLOCK")


def test_allow_payload_shape() -> None:
    p = allow_payload()
    assert p["tool_name"] == "Read"
    assert "/tmp/" in p["tool_args_json"]
    # Header has the trace dimensions the firewall expects.
    h = p["header"]
    for required in ("trace_id", "tenant_id", "aid", "timestamp_ns"):
        assert required in h


def test_approval_payload_targets_sensitive_path() -> None:
    p = approval_payload()
    assert "/etc/hosts" in p["tool_args_json"]


def test_block_payload_carries_destructive_command() -> None:
    p = block_payload()
    # Reassembled at runtime so this file's source text doesn't trip
    # an Aegis-installed pre-commit hook scanning for the literal.
    assert "kubectl" in p["tool_args_json"]
    assert "delete" in p["tool_args_json"]


def test_each_call_mints_fresh_aid() -> None:
    """Critical for soak runs — repeated identical aid would trip
    step336's loop detector and turn the load into a self-fulfilling
    REQUIRE_APPROVAL stampede."""
    aids = {allow_payload()["header"]["aid"] for _ in range(50)}
    assert len(aids) == 50


def test_each_call_mints_fresh_trace() -> None:
    traces = {allow_payload()["header"]["trace_id"] for _ in range(50)}
    assert len(traces) == 50


def test_payload_for_handles_zeroed_mix_gracefully() -> None:
    """Defensive: a zero-weight mix shouldn't crash; it falls back
    to a benign Read."""
    from aegis.soak.payloads import _MixEntry
    bad_mix = (_MixEntry(PayloadKind.ALLOW, allow_payload, 0.0, "ALLOW"),)
    kind, body, dec = payload_for(0.5, mix=bad_mix)
    assert kind == PayloadKind.ALLOW
    assert dec == "ALLOW"
    assert isinstance(body, dict)


def test_total_weight_in_default_mix_sums_to_one() -> None:
    total = sum(e.weight for e in PAYLOAD_MIX)
    # ±0.001 for floating point.
    assert abs(total - 1.0) < 0.001
