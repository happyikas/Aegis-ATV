"""Unit tests for the layered Burn-in controller (M11)."""

from __future__ import annotations

import time

from aegis.burnin import (
    SHADOW_FPR_MAX,
    SHADOW_MIN_SAMPLES,
    SHADOW_TPR_MIN,
    BurnInController,
    LayerKey,
    Phase,
    can_graduate,
)
from aegis.burnin.phases import PhaseMetrics, PhaseState
from aegis.schema import ATVHeader, ATVInput, Verdict


def _inp(tenant: str = "t", role: str = "r", aid: str = "a") -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t", span_id="s", tenant_id=tenant, aid=aid,
            timestamp_ns=time.time_ns(),
        ),
        plan_text="plan",
        tool_name="read_file",
        tool_args_json='{"path":"./data/x.txt"}',
        role_id=role,
    )


# ─────────────────────────────────────────────────────────────────────
# Phase / graduation
# ─────────────────────────────────────────────────────────────────────
class TestGraduation:
    def test_observation_needs_min_samples(self) -> None:
        s = PhaseState(current=Phase.OBSERVATION, metrics=PhaseMetrics(samples=10))
        ok, _ = can_graduate(s)
        assert not ok

    def test_observation_passes_at_threshold(self) -> None:
        s = PhaseState(current=Phase.OBSERVATION,
                       metrics=PhaseMetrics(samples=SHADOW_MIN_SAMPLES))
        ok, _ = can_graduate(s)
        assert ok

    def test_shadow_needs_all_three_gates(self) -> None:
        m = PhaseMetrics(
            true_positives=int(SHADOW_TPR_MIN * 100),
            false_negatives=int((1 - SHADOW_TPR_MIN) * 100),
            true_negatives=int((1 - SHADOW_FPR_MAX) * 100),
            false_positives=int(SHADOW_FPR_MAX * 100),
        )
        s = PhaseState(current=Phase.SHADOW, metrics=m)
        ok, _ = can_graduate(s)
        # tpr 0.95, fpr 0.02 satisfy first two; precision = 95/(95+2) ≈ 0.979 ≥ 0.90
        assert ok

    def test_shadow_blocks_on_low_precision(self) -> None:
        m = PhaseMetrics(true_positives=10, false_positives=90, false_negatives=0, true_negatives=100)
        # tpr=1.0, fpr=0.474, precision=0.10 → fail FPR + precision
        s = PhaseState(current=Phase.SHADOW, metrics=m)
        ok, _ = can_graduate(s)
        assert not ok

    def test_assisted_needs_low_override_rate(self) -> None:
        m = PhaseMetrics(human_overrides=2, human_total_decisions=100)
        s = PhaseState(current=Phase.ASSISTED, metrics=m)
        ok, _ = can_graduate(s)
        assert ok  # 2% ≤ 5% threshold

    def test_assisted_blocks_high_override(self) -> None:
        m = PhaseMetrics(human_overrides=20, human_total_decisions=100)
        s = PhaseState(current=Phase.ASSISTED, metrics=m)
        ok, _ = can_graduate(s)
        assert not ok


# ─────────────────────────────────────────────────────────────────────
# BurnInController
# ─────────────────────────────────────────────────────────────────────
class TestController:
    def test_observe_creates_5_layer_slots_per_call(self) -> None:
        c = BurnInController()
        c.observe(_inp(), Verdict(decision="ALLOW", reason="", atv_id="x"))
        st = c.status()
        # Exactly one slot per layer L1..L5 for this single (tenant, role, aid)
        assert {layer["layer"] for layer in st["layers"]} == {"L1", "L2", "L3", "L4", "L5"}

    def test_observe_increments_counts(self) -> None:
        c = BurnInController()
        for _ in range(7):
            c.observe(_inp(), Verdict(decision="ALLOW", reason="", atv_id="x"))
        st = c.status()
        for layer in st["layers"]:
            assert layer["samples"] == 7

    def test_record_label_updates_tp_fp_tn_fn(self) -> None:
        c = BurnInController()
        inp = _inp()
        # ALLOW vs malicious → false negative
        c.record_label(
            inp,
            Verdict(decision="ALLOW", reason="", atv_id="x"),
            ground_truth="malicious",
        )
        # BLOCK vs malicious → true positive
        c.record_label(
            inp,
            Verdict(decision="BLOCK", reason="", atv_id="x"),
            ground_truth="malicious",
        )
        # BLOCK vs benign → false positive
        c.record_label(
            inp,
            Verdict(decision="BLOCK", reason="", atv_id="x"),
            ground_truth="benign",
        )
        # ALLOW vs benign → true negative
        c.record_label(
            inp,
            Verdict(decision="ALLOW", reason="", atv_id="x"),
            ground_truth="benign",
        )
        st = c.status()
        l1 = next(layer for layer in st["layers"] if layer["layer"] == "L1")
        assert l1["tpr"] == 0.5         # 1 TP / (1 TP + 1 FN)
        assert round(l1["fpr"], 4) == 0.5  # 1 FP / (1 FP + 1 TN)

    def test_try_graduate_rejects_under_threshold(self) -> None:
        c = BurnInController()
        c.observe(_inp(), Verdict(decision="ALLOW", reason="", atv_id="x"))
        ok, _ = c.try_graduate(LayerKey("L1").as_str())
        assert not ok  # only 1 sample, far below SHADOW_MIN_SAMPLES

    def test_try_graduate_advances_at_threshold(self) -> None:
        c = BurnInController()
        for _ in range(SHADOW_MIN_SAMPLES):
            c.observe(_inp(), Verdict(decision="ALLOW", reason="", atv_id="x"))
        ok, _ = c.try_graduate(LayerKey("L1").as_str())
        assert ok
        # state was bumped
        st = c.status()
        l1 = next(layer for layer in st["layers"] if layer["layer"] == "L1")
        assert l1["phase"] == Phase.SHADOW.value
        assert len(l1["transitions"]) == 1

    def test_composite_score_zero_in_observation(self) -> None:
        c = BurnInController()
        for _ in range(50):
            c.observe(_inp(), Verdict(decision="ALLOW", reason="", atv_id="x"))
        # All slots still in OBSERVATION → score should be 0
        assert c.composite_score(_inp()) == 0.0

    def test_composite_score_rises_after_graduation(self) -> None:
        c = BurnInController()
        for _ in range(SHADOW_MIN_SAMPLES):
            c.observe(_inp(), Verdict(decision="ALLOW", reason="", atv_id="x"))
        # graduate L1..L5 to SHADOW
        st = c.status()
        for layer in st["layers"]:
            c.try_graduate(layer["key"])
        score = c.composite_score(_inp())
        # Each layer contributes weight × phase_factor × saturation. With
        # SHADOW_MIN_SAMPLES samples, L1 (1k expected) saturates at 1.0;
        # L2 (5k) at 0.2; L3 (2k) at 0.5; L4 (1k) at 1.0; L5 (500) at 1.0.
        # Sum = 0.2 × 0.4 × (1.0 + 0.2 + 0.5 + 1.0 + 1.0) = 0.296.
        assert 0.25 <= score <= 0.40

    def test_event_firmware_upgrade_resets_l1(self) -> None:
        c = BurnInController()
        for _ in range(50):
            c.observe(_inp(), Verdict(decision="ALLOW", reason="", atv_id="x"))
        c.event_firmware_upgrade()
        st = c.status()
        l1 = next(layer for layer in st["layers"] if layer["layer"] == "L1")
        assert l1["samples"] == 0  # reset

    def test_layer_keys_distinguish_tenants_and_roles(self) -> None:
        c = BurnInController()
        c.observe(_inp(tenant="t1", role="r1"),
                  Verdict(decision="ALLOW", reason="", atv_id="x"))
        c.observe(_inp(tenant="t2", role="r1"),
                  Verdict(decision="ALLOW", reason="", atv_id="x"))
        c.observe(_inp(tenant="t1", role="r2"),
                  Verdict(decision="ALLOW", reason="", atv_id="x"))
        st = c.status()
        # L4 keyed by (tenant, role) → 3 distinct slots
        l4_keys = {layer["key"] for layer in st["layers"] if layer["layer"] == "L4"}
        assert len(l4_keys) == 3
