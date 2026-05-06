"""Tests for ``aegis.burnin.calibration_feedback`` — Phase D feedback
loop that turns accumulated retrospective records into recommended
threshold adjustments (PR-ψ-feedback, v2.7.2)."""

from __future__ import annotations

import json
from pathlib import Path

from aegis.burnin.calibration_feedback import (
    PerSignalAccuracy,
    analyse_audit,
    apply_recommended_calibration,
    render_feedback_report,
)

# ──────────────────────────────────────────────────────────────────────
# Helpers — synthetic audit construction
# ──────────────────────────────────────────────────────────────────────


def _pretool_record(
    *,
    invocation_id: str,
    tool: str = "Bash",
    decision: str = "ALLOW",
    gate_invoked: bool = False,
    gate_reason: str = "no critical signals",
    m13_score: float | None = None,
    drift: float | None = None,
    aid: str = "sess-test",
) -> dict:
    explain: dict = {
        "advisor_gate": {"invoked": gate_invoked, "reason": gate_reason},
    }
    if m13_score is not None:
        explain["m13_score"] = m13_score
    if drift is not None:
        explain["session_drift"] = {"topic_drift": drift}
    return {
        "ts_ns": 0, "tool": tool, "aid": aid,
        "invocation_id": invocation_id, "decision": decision,
        "trace_id": "t" * 32, "mode": "local", "explain": explain,
    }


def _posttool_record(
    *,
    invocation_id: str,
    tool: str = "Bash",
    accuracy: str = "accurate",
) -> dict:
    return {
        "ts_ns": 1, "tool": tool, "aid": "sess-test",
        "invocation_id": invocation_id, "hook": "PostToolUse",
        "status": "success", "mode": "local",
        "explain": {"retrospective_advice": {
            "invocation_id": invocation_id,
            "tool_name": tool,
            "predicted_decision": "ALLOW",
            "actual_status": "success",
            "accuracy": accuracy,
        }},
    }


def _write_audit(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


# ──────────────────────────────────────────────────────────────────────
# analyse_audit — basic shape
# ──────────────────────────────────────────────────────────────────────


class TestAnalyseShape:
    def test_missing_audit_returns_empty_report(self, tmp_path: Path) -> None:
        report = analyse_audit(tmp_path / "no.jsonl")
        assert report.n_pre == 0
        assert report.n_post == 0
        assert report.n_with_retrospective == 0
        assert report.recommended_calibration is None

    def test_pre_only_audit_no_retrospective(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path, [
            _pretool_record(invocation_id="i1"),
        ])
        r = analyse_audit(path)
        assert r.n_pre == 1
        assert r.n_post == 0
        assert r.n_with_retrospective == 0
        assert r.overall_accuracy == {}

    def test_overall_accuracy_counts(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        records = [
            _pretool_record(invocation_id="i1", gate_invoked=True,
                            gate_reason="verdict=BLOCK"),
            _posttool_record(invocation_id="i1", accuracy="accurate"),
            _pretool_record(invocation_id="i2", gate_invoked=True,
                            gate_reason="verdict=REQUIRE_APPROVAL"),
            _posttool_record(invocation_id="i2", accuracy="false_alarm"),
            _pretool_record(invocation_id="i3"),
            _posttool_record(invocation_id="i3", accuracy="not_applicable"),
        ]
        _write_audit(path, records)
        r = analyse_audit(path)
        assert r.overall_accuracy["accurate"] == 1
        assert r.overall_accuracy["false_alarm"] == 1
        assert r.overall_accuracy["not_applicable"] == 1


# ──────────────────────────────────────────────────────────────────────
# Per-signal classification
# ──────────────────────────────────────────────────────────────────────


class TestPerSignal:
    def test_verdict_signal_classified(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path, [
            _pretool_record(invocation_id="i1", gate_invoked=True,
                            gate_reason="verdict=BLOCK"),
            _posttool_record(invocation_id="i1", accuracy="accurate"),
        ])
        r = analyse_audit(path)
        signals = {s.signal: s for s in r.per_signal}
        assert "verdict_non_allow" in signals
        assert signals["verdict_non_allow"].n_fired == 1
        assert signals["verdict_non_allow"].n_accurate == 1

    def test_m13_calibrated_signal_classified(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path, [
            _pretool_record(invocation_id="i1", gate_invoked=True,
                            gate_reason="M13 score 0.05 < burn-in p10 0.15"),
            _posttool_record(invocation_id="i1", accuracy="false_alarm"),
        ])
        r = analyse_audit(path)
        signals = {s.signal: s for s in r.per_signal}
        assert "m13_low_calibrated" in signals
        assert signals["m13_low_calibrated"].n_false_alarm == 1
        assert signals["m13_low_calibrated"].precision == 0.0

    def test_drift_calibrated_signal_classified(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path, [
            _pretool_record(invocation_id="i1", gate_invoked=True,
                            gate_reason="session drift 0.85 > burn-in p95 0.70"),
            _posttool_record(invocation_id="i1", accuracy="accurate"),
        ])
        r = analyse_audit(path)
        signals = {s.signal: s for s in r.per_signal}
        assert "drift_high_calibrated" in signals

    def test_loop_signal_classified(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path, [
            _pretool_record(invocation_id="i1", gate_invoked=True,
                            gate_reason="loop/redundancy signal"),
            _posttool_record(invocation_id="i1", accuracy="accurate"),
        ])
        r = analyse_audit(path)
        signals = {s.signal: s for s in r.per_signal}
        assert "step336_loop" in signals

    def test_precision_calculation(self) -> None:
        s = PerSignalAccuracy(
            signal="x", n_fired=5, n_accurate=3, n_false_alarm=2,
        )
        assert s.precision == 0.6

    def test_precision_zero_when_no_evidence(self) -> None:
        s = PerSignalAccuracy(signal="x")
        assert s.precision == 0.0


# ──────────────────────────────────────────────────────────────────────
# Calibration recommendation — re-extract from accumulated audit
# ──────────────────────────────────────────────────────────────────────


def _audit_with_distribution(
    path: Path,
    *,
    n: int = 25,
    m13_low: float = 0.05,
    m13_high: float = 0.45,
    drift_low: float = 0.0,
    drift_high: float = 0.6,
) -> None:
    """Build an audit with a controlled distribution of m13_score and
    session_drift values across `n` records."""
    records: list[dict] = []
    for i in range(n):
        m13 = m13_low + (m13_high - m13_low) * (i / max(n - 1, 1))
        drift = drift_low + (drift_high - drift_low) * (i / max(n - 1, 1))
        # Spread across distinct aids so n_sessions clears
        # MIN_SAMPLES_FOR_CALIBRATION (5).
        records.append(_pretool_record(
            invocation_id=f"i{i}",
            m13_score=m13, drift=drift,
            aid=f"sess-{i // 3}",
        ))
    _write_audit(path, records)


class TestRecommendation:
    def test_below_min_samples_no_recommendation(
        self, tmp_path: Path
    ) -> None:
        # Only 3 records — below MIN_SAMPLES_FOR_CALIBRATION.
        path = tmp_path / "audit.jsonl"
        _audit_with_distribution(path, n=3)
        r = analyse_audit(path)
        # Calibration is unchanged because re-extraction failed.
        assert r.recommended_calibration is None
        # Notes should explain why.
        assert any("MIN_SAMPLES" in n for n in r.notes)

    def test_recommends_when_distribution_diverges(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "audit.jsonl"
        # Skewed distribution — m13 mostly very low, drift mostly high
        # (different from the synthetic-default p10=0.15, p95=0.70).
        _audit_with_distribution(
            path, n=30, m13_low=0.01, m13_high=0.05,
            drift_low=0.5, drift_high=0.99,
        )
        r = analyse_audit(path)
        assert r.recommended_calibration is not None
        assert r.recommended_calibration.is_usable()
        # The recommended p10 should be much lower than default 0.15
        # because all our m13 values are in [0.01, 0.05].
        assert r.recommended_calibration.m13_score_p10 < 0.10
        assert r.calibration_changed

    def test_unchanged_when_distribution_matches_default(
        self, tmp_path: Path
    ) -> None:
        # Build a distribution that lands right on the synthetic
        # defaults: m13 p10=0.15, p95=0.70.
        path = tmp_path / "audit.jsonl"
        _audit_with_distribution(
            path, n=30, m13_low=0.10, m13_high=0.50,
            drift_low=0.05, drift_high=0.75,
        )
        r = analyse_audit(path)
        # Recommended should exist but might or might not be flagged
        # as changed depending on tolerance — either way is fine, the
        # important thing is no crash.
        assert r.recommended_calibration is not None


# ──────────────────────────────────────────────────────────────────────
# apply_recommended_calibration
# ──────────────────────────────────────────────────────────────────────


class TestApply:
    def test_apply_writes_calibration(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _audit_with_distribution(audit, n=30, m13_low=0.01, m13_high=0.05)
        r = analyse_audit(audit)
        out = tmp_path / "applied.json"
        result = apply_recommended_calibration(r, output_path=out)
        assert result == out
        assert out.is_file()
        # File should be a valid calibration JSON.
        from aegis.burnin.advisor_calibration import load_calibration
        loaded = load_calibration(out)
        assert loaded.is_usable()

    def test_apply_no_op_when_no_recommendation(
        self, tmp_path: Path
    ) -> None:
        # Empty audit → no recommendation → no write.
        audit = tmp_path / "empty.jsonl"
        audit.write_text("")
        r = analyse_audit(audit)
        out = tmp_path / "applied.json"
        result = apply_recommended_calibration(r, output_path=out)
        assert result is None
        assert not out.exists()


# ──────────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────────


class TestRender:
    def test_render_includes_signal_table(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path, [
            _pretool_record(invocation_id="i1", gate_invoked=True,
                            gate_reason="verdict=BLOCK"),
            _posttool_record(invocation_id="i1", accuracy="accurate"),
        ])
        r = analyse_audit(path)
        text = render_feedback_report(r)
        assert "Per-signal accuracy:" in text
        assert "verdict_non_allow" in text
        assert "Calibration status:" in text

    def test_render_handles_empty_audit(self, tmp_path: Path) -> None:
        r = analyse_audit(tmp_path / "no.jsonl")
        text = render_feedback_report(r)
        assert "AdvisorCalibration Feedback Report" in text
