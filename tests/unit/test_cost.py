"""Unit tests for the cost package — M12 (Claims 3, 26, 27, 30, 34)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from aegis.cost import (
    CostAttestationLedger,
    DivergenceMetrics,
    compute_divergence,
    dollar_cost_divergence,
    evaluate_escalation,
    expected_dollars,
    expected_flops,
    memory_cost_divergence,
    token_to_flops_divergence,
)
from aegis.cost.model_flops import FLOPS_PER_TOKEN
from aegis.schema import ATVHeader, CostEfficiencyMetrics
from aegis.sign.ed25519 import load_or_create_key


def _hdr(aid: str = "a", tenant: str = "t") -> ATVHeader:
    return ATVHeader(
        trace_id="t", span_id="s", tenant_id=tenant, aid=aid,
        timestamp_ns=time.time_ns(),
    )


# ─────────────────────────────────────────────────────────────────────
# model_flops
# ─────────────────────────────────────────────────────────────────────
class TestModelFlops:
    def test_known_model_returns_pair(self) -> None:
        ifl, ofl = FLOPS_PER_TOKEN["claude-haiku-4-5"]
        assert ifl > 0 and ofl > 0

    def test_unknown_model_falls_back_to_default(self) -> None:
        f = expected_flops("not-a-real-model", input_tokens=100, output_tokens=50)
        in_def, out_def = FLOPS_PER_TOKEN["default"]
        assert f == 100 * in_def + 50 * out_def

    def test_expected_dollars_scales_linearly(self) -> None:
        d1 = expected_dollars("default", input_tokens=100, output_tokens=0)
        d2 = expected_dollars("default", input_tokens=200, output_tokens=0)
        assert d2 == pytest.approx(2 * d1)


# ─────────────────────────────────────────────────────────────────────
# divergence (T2: HW=0 → 0)
# ─────────────────────────────────────────────────────────────────────
class TestDivergenceT2:
    def test_token_to_flops_zero_when_hw_absent(self) -> None:
        m = CostEfficiencyMetrics(input_token_count=100, output_token_count=50)
        assert token_to_flops_divergence(m, model_name="default", hw_flops_observed=0) == 0.0

    def test_memory_zero_when_hw_absent(self) -> None:
        m = CostEfficiencyMetrics(cumulative_tokens=1000)
        assert memory_cost_divergence(m, hw_hbm_bytes_observed=0) == 0.0

    def test_dollar_zero_when_hw_absent(self) -> None:
        m = CostEfficiencyMetrics(cumulative_dollars=0.05)
        assert dollar_cost_divergence(
            m, model_name="default", hw_flops_observed=0,
        ) == 0.0

    def test_compute_divergence_returns_zeros_in_t2(self) -> None:
        m = CostEfficiencyMetrics(input_token_count=100, output_token_count=50,
                                  cumulative_tokens=500, cumulative_dollars=0.05)
        d = compute_divergence(m, model_name="default")
        assert (d.token_to_flops, d.memory_cost, d.dollar_cost) == (0.0, 0.0, 0.0)


# ─────────────────────────────────────────────────────────────────────
# divergence (synthetic HW: math sanity)
# ─────────────────────────────────────────────────────────────────────
class TestDivergenceWithSyntheticHW:
    def test_token_to_flops_nonzero_with_hw(self) -> None:
        m = CostEfficiencyMetrics(input_token_count=100, output_token_count=50)
        # SW expects exact value; HW observes 2x → divergence = (2−1)/1 = 1.0 capped
        sw_expected = expected_flops("default", 100, 50)
        d = token_to_flops_divergence(m, model_name="default",
                                      hw_flops_observed=sw_expected * 2)
        assert d == pytest.approx(1.0)  # clamped to 1.0

    def test_token_to_flops_small_drift(self) -> None:
        m = CostEfficiencyMetrics(input_token_count=100, output_token_count=50)
        sw_expected = expected_flops("default", 100, 50)
        d = token_to_flops_divergence(m, model_name="default",
                                      hw_flops_observed=sw_expected * 1.05)
        assert 0.04 <= d <= 0.06  # ~5%

    def test_memory_cost_with_hw(self) -> None:
        m = CostEfficiencyMetrics(cumulative_tokens=1000)
        # 256 bytes/token → 256000 expected; observe 320000 (25% higher)
        d = memory_cost_divergence(m, hw_hbm_bytes_observed=320_000)
        assert 0.20 <= d <= 0.30

    def test_dollar_cost_with_hw(self) -> None:
        m = CostEfficiencyMetrics(cumulative_dollars=0.10)
        # SW says $0.10. HW-derived = flops * coef. Use synthetic flops that
        # back-computes to $0.05 → divergence = (0.10 - 0.05) / 0.10 = 0.5
        flops = 0.05 / 1e-15  # = 5e13
        d = dollar_cost_divergence(m, model_name="default",
                                   hw_flops_observed=flops)
        assert 0.45 <= d <= 0.55


# ─────────────────────────────────────────────────────────────────────
# escalation
# ─────────────────────────────────────────────────────────────────────
class TestEscalation:
    def test_no_escalation_below_threshold(self) -> None:
        d = DivergenceMetrics(token_to_flops=0.05, memory_cost=0.0, dollar_cost=0.0)
        decision = evaluate_escalation(d)
        assert decision.triggered is False

    def test_escalation_when_token_flops_exceeds(self) -> None:
        # token_to_flops baseline=0.10; threshold = 0.10 × 3.0 = 0.30
        d = DivergenceMetrics(token_to_flops=0.50, memory_cost=0.0, dollar_cost=0.0)
        decision = evaluate_escalation(d)
        assert decision.triggered is True
        assert decision.metric == "token_to_flops"
        assert "tampering" in decision.reason.lower() or "model substitution" in decision.reason.lower()

    def test_escalation_when_memory_exceeds(self) -> None:
        d = DivergenceMetrics(token_to_flops=0.0, memory_cost=0.5, dollar_cost=0.0)
        decision = evaluate_escalation(d)
        assert decision.triggered is True
        assert decision.metric == "memory_cost"

    def test_escalation_when_dollar_exceeds(self) -> None:
        d = DivergenceMetrics(token_to_flops=0.0, memory_cost=0.0, dollar_cost=0.5)
        decision = evaluate_escalation(d)
        assert decision.triggered is True
        assert decision.metric == "dollar_cost"

    def test_role_baseline_override_relaxes_threshold(self) -> None:
        # Looser baseline → no escalation even at 0.5
        d = DivergenceMetrics(token_to_flops=0.5, memory_cost=0.0, dollar_cost=0.0)
        decision = evaluate_escalation(
            d, role_baseline={"token_to_flops": 0.30},  # threshold = 0.90
        )
        assert decision.triggered is False


# ─────────────────────────────────────────────────────────────────────
# Cost ledger — sign + chain + verify
# ─────────────────────────────────────────────────────────────────────
class TestCostLedger:
    def _ledger(self, tmp_path: Path) -> tuple[CostAttestationLedger, object]:
        key = load_or_create_key(tmp_path / "ed25519_cost.pem")
        ledger = CostAttestationLedger(
            db_path=":memory:",
            jsonl_path=tmp_path / "cost.jsonl",
            signing_key=key,
        )
        return ledger, key

    def test_append_signs_and_chains(self, tmp_path: Path) -> None:
        ledger, _ = self._ledger(tmp_path)
        m = CostEfficiencyMetrics(input_token_count=100, cumulative_dollars=0.01)
        d = DivergenceMetrics(0.0, 0.0, 0.0)
        rec = ledger.append(
            atv_commitment="abc" * 21 + "f",
            header=_hdr(),
            sw_cost_metrics=m,
            divergence=d,
            model_name="claude-haiku-4-5",
        )
        assert rec["this_hash"]
        assert rec["signature"]
        assert rec["prev_hash"] == "GENESIS"
        assert rec["model_name"] == "claude-haiku-4-5"

    def test_chain_links_across_records(self, tmp_path: Path) -> None:
        ledger, _ = self._ledger(tmp_path)
        m = CostEfficiencyMetrics(input_token_count=100)
        d = DivergenceMetrics(0.0, 0.0, 0.0)
        a = ledger.append(atv_commitment="x" * 64, header=_hdr(),
                          sw_cost_metrics=m, divergence=d)
        b = ledger.append(atv_commitment="y" * 64, header=_hdr(),
                          sw_cost_metrics=m, divergence=d)
        assert b["prev_hash"] == a["this_hash"]

    def test_verify_chain_passes_for_clean_chain(self, tmp_path: Path) -> None:
        ledger, _ = self._ledger(tmp_path)
        m = CostEfficiencyMetrics(input_token_count=100)
        d = DivergenceMetrics(0.0, 0.0, 0.0)
        for i in range(5):
            ledger.append(atv_commitment=str(i) * 64, header=_hdr(),
                          sw_cost_metrics=m, divergence=d)
        ok, err = ledger.verify_chain("a")
        assert ok and err is None

    def test_signature_round_trip(self, tmp_path: Path) -> None:
        """A verifier with the cost public key + the record can confirm."""
        ledger, key = self._ledger(tmp_path)
        m = CostEfficiencyMetrics(input_token_count=42, cumulative_dollars=0.01)
        d = DivergenceMetrics(0.05, 0.0, 0.0)
        rec = ledger.append(
            atv_commitment="z" * 64, header=_hdr(),
            sw_cost_metrics=m, divergence=d, model_name="default",
        )
        # Recompute canonical bytes and verify against the cost public key.
        import json as _json
        payload = {k: v for k, v in rec.items()
                   if k not in ("this_hash", "signature", "algorithm")}
        canonical = _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        pub = key.public_key()
        # Should not raise.
        pub.verify(bytes.fromhex(rec["signature"]), canonical)

    def test_atv_commitment_persisted(self, tmp_path: Path) -> None:
        """Claim 30: cost record carries an ATV commitment so a verifier
        with the (later disclosed) ATV can confirm cost wasn't tampered with."""
        ledger, _ = self._ledger(tmp_path)
        m = CostEfficiencyMetrics(input_token_count=1)
        commit = "deadbeef" * 8
        rec = ledger.append(atv_commitment=commit, header=_hdr(),
                            sw_cost_metrics=m, divergence=DivergenceMetrics(0.0, 0.0, 0.0))
        assert rec["atv_commitment"] == commit
        roundtrip = ledger.get(rec["record_id"])
        assert roundtrip["atv_commitment"] == commit

    def test_separate_signing_key_from_audit_key(self, tmp_path: Path) -> None:
        """Claim 34: the cost-attestation key slot is DISTINCT from the
        telemetry signing key."""
        cost_key = load_or_create_key(tmp_path / "ed25519_cost.pem")
        audit_key = load_or_create_key(tmp_path / "ed25519.pem")
        cost_pub_raw = cost_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        audit_pub_raw = audit_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        assert cost_pub_raw != audit_pub_raw
