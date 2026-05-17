"""Tests for v0.5.23 — ATV centroid Mahalanobis-distance gate.

Three layers:

1. Centroid math — feature extraction, mean/var aggregation,
   Mahalanobis distance correctness.
2. Learner integration — built trust tables carry populated
   centroids when clean records have cost/tokens/latency.
3. Runtime gate — `evaluate_autonomy_request` refuses bypass
   when the runtime fingerprint is outside the cluster.
"""

from __future__ import annotations

import math
import time

import pytest

from aegis.autonomy.centroid import (
    DEFAULT_MAHALANOBIS_THRESHOLD,
    FEATURE_DIM,
    compute_centroid_and_cov,
    feature_vector,
    feature_vector_from_signals,
    is_outside_cluster,
    mahalanobis_distance_diag,
)
from aegis.autonomy.learner import (
    TrustedPattern,
    evaluate_autonomy_request,
    learn_with_diagnostics,
)
from aegis.context_memory.record import ContextMemoryRecord


def _rec(
    *,
    aid: str = "agent-A",
    tool: str = "Bash",
    decision: str = "REQUIRE_APPROVAL",
    reason: str = "same Bash call repeated 3 times this session",
    trace_id: str = "t",
    ts_ns: int = 0,
    cost_usd: float = 0.001,
    tokens_in: int = 100,
    latency_ms: float = 100.0,
) -> ContextMemoryRecord:
    return ContextMemoryRecord(
        ts_ns=ts_ns or time.time_ns(),
        trace_id=trace_id,
        invocation_id="inv",
        aid=aid,
        tenant_id="t",
        tool_name=tool,
        decision=decision,
        reason=reason,
        channel=None,
        provider=None,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        tokens_in=tokens_in,
        tokens_out=50,
        step_traces={},
        m13_score=None,
        advisor_invoked=False,
        recommended_advisors=(),
        atv_sha3=None,
        atv_dim=2080,
        is_sidechain=False,
        mode="sidecar",
    )


# ──────────────────────────────────────────────────────────────────
# 1. Centroid math
# ──────────────────────────────────────────────────────────────────


class TestFeatureExtraction:
    def test_dim_is_3(self) -> None:
        assert FEATURE_DIM == 3

    def test_feature_vector_from_record(self) -> None:
        rec = _rec(cost_usd=0.01, tokens_in=200, latency_ms=150.0)
        fv = feature_vector(rec)
        assert len(fv) == 3
        # log scale: log(0.01 + 1e-6) ≈ -4.6, log(201) ≈ 5.3,
        # log(151) ≈ 5.0.
        assert fv[0] < 0  # cost is < 1, log negative
        assert fv[1] > 0  # tokens > 1
        assert fv[2] > 0  # latency > 1ms

    def test_feature_vector_from_signals_matches(self) -> None:
        rec = _rec(cost_usd=0.05, tokens_in=300, latency_ms=200.0)
        fv1 = feature_vector(rec)
        fv2 = feature_vector_from_signals(
            cost_usd=0.05, tokens_in=300, latency_ms=200.0,
        )
        for a, b in zip(fv1, fv2, strict=True):
            assert a == pytest.approx(b)


class TestCentroidComputation:
    def test_empty_returns_empty(self) -> None:
        assert compute_centroid_and_cov([]) == ((), ())

    def test_single_sample_zero_variance(self) -> None:
        centroid, cov = compute_centroid_and_cov([(1.0, 2.0, 3.0)])
        assert centroid == (1.0, 2.0, 3.0)
        assert cov == (0.0, 0.0, 0.0)

    def test_two_samples_correct_mean(self) -> None:
        centroid, cov = compute_centroid_and_cov(
            [(0.0, 0.0, 0.0), (2.0, 4.0, 6.0)],
        )
        assert centroid == (1.0, 2.0, 3.0)
        # population variance: each dim has values centred at mean,
        # squared deviation = 1, 4, 9 → /n=2 → 1, 4, 9.
        assert cov == pytest.approx((1.0, 4.0, 9.0))

    def test_mismatched_dim_dropped(self) -> None:
        centroid, cov = compute_centroid_and_cov(
            [(1.0, 2.0, 3.0), (4.0, 5.0)],  # second row wrong dim
        )
        # Only the first row contributes.
        assert centroid == (1.0, 2.0, 3.0)


class TestMahalanobisDistance:
    def test_zero_when_point_is_centroid(self) -> None:
        d = mahalanobis_distance_diag(
            (1.0, 2.0, 3.0),
            (1.0, 2.0, 3.0),
            (0.5, 0.5, 0.5),
        )
        assert d == 0.0

    def test_unit_sigma_euclidean(self) -> None:
        """With cov=identity, Mahalanobis = Euclidean."""
        d = mahalanobis_distance_diag(
            (3.0, 4.0, 0.0),
            (0.0, 0.0, 0.0),
            (1.0, 1.0, 1.0),
        )
        # sqrt(9 + 16 + 0) = 5.
        assert d == pytest.approx(5.0)

    def test_inf_on_dim_mismatch(self) -> None:
        d = mahalanobis_distance_diag(
            (1.0, 2.0),
            (0.0, 0.0, 0.0),
            (1.0, 1.0, 1.0),
        )
        assert math.isinf(d)

    def test_inf_on_empty_centroid(self) -> None:
        d = mahalanobis_distance_diag((1.0, 2.0, 3.0), (), ())
        assert math.isinf(d)

    def test_zero_variance_floored(self) -> None:
        """sigma=0 should be floored to epsilon so distance is
        large but finite (not divide-by-zero)."""
        d = mahalanobis_distance_diag(
            (1.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (0.0, 1.0, 1.0),
        )
        assert d > 100.0
        assert not math.isinf(d)


class TestIsOutsideCluster:
    def test_no_centroid_returns_false(self) -> None:
        # No centroid yet → don't gate.
        assert is_outside_cluster((1.0, 2.0, 3.0), (), ()) is False

    def test_point_at_centroid_inside(self) -> None:
        assert is_outside_cluster(
            (1.0, 2.0, 3.0),
            (1.0, 2.0, 3.0),
            (0.5, 0.5, 0.5),
        ) is False

    def test_far_point_outside(self) -> None:
        # 10σ away.
        assert is_outside_cluster(
            (10.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (1.0, 1.0, 1.0),
            threshold=3.0,
        ) is True


# ──────────────────────────────────────────────────────────────────
# 2. Learner integration
# ──────────────────────────────────────────────────────────────────


class TestLearnerCollectsCentroid:
    def test_centroid_populated_after_learn(self) -> None:
        base_ts = time.time_ns() - 1_000_000_000
        recs = [
            _rec(
                trace_id=f"loop-{i:04d}",
                aid=f"agent-{i % 3}",
                ts_ns=base_ts + i,
                cost_usd=0.001 * (1 + i * 0.1),
                tokens_in=100 + i,
                latency_ms=50.0 + i * 2.0,
            )
            for i in range(30)
        ]
        result = learn_with_diagnostics(recs, min_samples=5)
        if ("Bash", "loop:Bash") not in result.trust_table:
            pytest.skip("pattern below admission threshold")
        p = result.trust_table[("Bash", "loop:Bash")]
        # Centroid should have 3 dims; n_samples should reflect
        # the clean records.
        assert len(p.atv_centroid) == 3
        assert len(p.atv_cov_diag) == 3
        assert p.centroid_n_samples > 0


# ──────────────────────────────────────────────────────────────────
# 3. Runtime gate
# ──────────────────────────────────────────────────────────────────


def _trusted_with_centroid(
    centroid: tuple[float, ...] = (1.0, 5.0, 5.0),
    cov: tuple[float, ...] = (0.1, 0.1, 0.1),
    n_samples: int = 50,
) -> TrustedPattern:
    return TrustedPattern(
        tool_name="Bash",
        reason_signature="loop:Bash",
        n_seen=200,
        n_followed_by_block=0,
        clean_rate=1.0,
        trust_score=0.99,
        last_seen_ns=time.time_ns(),
        alpha=201.0,
        beta=1.0,
        n_effective=200.0,
        atv_centroid=centroid,
        atv_cov_diag=cov,
        centroid_n_samples=n_samples,
    )


class TestRuntimeCentroidGate:
    def test_centroid_skipped_when_no_features_supplied(self) -> None:
        pattern = _trusted_with_centroid()
        av = evaluate_autonomy_request(
            tool_name="Bash",
            reason="same Bash call repeated 3 times this session",
            trust_table={pattern.key: pattern},
        )
        # No runtime_features → centroid gate doesn't fire.
        assert av.auto_approve is True

    def test_centroid_skipped_with_insufficient_samples(self) -> None:
        pattern = _trusted_with_centroid(n_samples=10)  # < default 20
        av = evaluate_autonomy_request(
            tool_name="Bash",
            reason="same Bash call repeated 3 times this session",
            trust_table={pattern.key: pattern},
            runtime_features=(100.0, 100.0, 100.0),  # far point
        )
        # Below the n_samples floor → centroid gate skipped.
        assert av.auto_approve is True

    def test_inside_cluster_bypasses(self) -> None:
        pattern = _trusted_with_centroid(
            centroid=(1.0, 5.0, 5.0),
            cov=(0.1, 0.1, 0.1),
        )
        av = evaluate_autonomy_request(
            tool_name="Bash",
            reason="same Bash call repeated 3 times this session",
            trust_table={pattern.key: pattern},
            runtime_features=(1.0, 5.0, 5.0),  # exactly at centroid
        )
        assert av.auto_approve is True

    def test_outside_cluster_refuses(self) -> None:
        pattern = _trusted_with_centroid(
            centroid=(1.0, 5.0, 5.0),
            cov=(0.1, 0.1, 0.1),
        )
        av = evaluate_autonomy_request(
            tool_name="Bash",
            reason="same Bash call repeated 3 times this session",
            trust_table={pattern.key: pattern},
            runtime_features=(100.0, 100.0, 100.0),  # very far
        )
        assert av.auto_approve is False
        assert "centroid_outlier" in av.outlier_signals

    def test_drift_takes_priority_over_centroid(self) -> None:
        """A drifted pattern is refused with the drift signal even
        if the centroid is populated."""
        pattern = TrustedPattern(
            tool_name="Bash",
            reason_signature="loop:Bash",
            n_seen=200,
            n_followed_by_block=0,
            clean_rate=1.0,
            trust_score=0.99,
            last_seen_ns=time.time_ns(),
            drifted=True,
            atv_centroid=(1.0, 5.0, 5.0),
            atv_cov_diag=(0.1, 0.1, 0.1),
            centroid_n_samples=50,
        )
        av = evaluate_autonomy_request(
            tool_name="Bash",
            reason="same Bash call repeated 3 times this session",
            trust_table={pattern.key: pattern},
            runtime_features=(1.0, 5.0, 5.0),
        )
        assert av.auto_approve is False
        assert "drift_detected" in av.outlier_signals
        assert "centroid_outlier" not in av.outlier_signals


class TestThresholdConstant:
    def test_default_is_three_sigma(self) -> None:
        assert DEFAULT_MAHALANOBIS_THRESHOLD == 3.0
