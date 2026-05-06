"""Tests for ``aegis.burnin.advisor_calibration`` — burn-in derived
percentile thresholds for the M13 confidence and session-drift signals
in the v2.6 advisor gate (PR-ψ-calibration)."""

from __future__ import annotations

import json
from pathlib import Path

from aegis.burnin.advisor_calibration import (
    MIN_SAMPLES_FOR_CALIBRATION,
    AdvisorCalibration,
    calibration_from_dict,
    calibration_to_dict,
    default_calibration,
    default_calibration_path,
    extract_calibration_from_audit,
    load_calibration,
    load_calibration_or_default,
    render_calibration,
    save_calibration,
)

# ──────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────


class TestSchema:
    def test_default_is_usable(self) -> None:
        c = default_calibration()
        assert c.is_usable()
        assert c.n_sessions >= MIN_SAMPLES_FOR_CALIBRATION

    def test_default_thresholds_in_unit_interval(self) -> None:
        c = default_calibration()
        for v in (c.m13_score_p10, c.m13_score_p25, c.m13_score_p50):
            assert 0.0 <= v <= 1.0
        for v in (c.topic_drift_p50, c.topic_drift_p75,
                  c.topic_drift_p90, c.topic_drift_p95):
            assert 0.0 <= v <= 1.0

    def test_m13_percentiles_monotonic(self) -> None:
        c = default_calibration()
        assert c.m13_score_p10 <= c.m13_score_p25 <= c.m13_score_p50

    def test_drift_percentiles_monotonic(self) -> None:
        c = default_calibration()
        assert (
            c.topic_drift_p50 <= c.topic_drift_p75
            <= c.topic_drift_p90 <= c.topic_drift_p95
        )

    def test_under_sampled_calibration_not_usable(self) -> None:
        c = AdvisorCalibration(
            version=1, n_sessions=2, extracted_at_ns=0,
            extracted_from="synth",
            m13_score_n=2, m13_score_p10=0.1, m13_score_p25=0.2,
            m13_score_p50=0.3,
            topic_drift_n=2, topic_drift_p50=0.0,
            topic_drift_p75=0.1, topic_drift_p90=0.2,
            topic_drift_p95=0.3,
        )
        assert not c.is_usable()

    def test_stable_hash_is_64_hex(self) -> None:
        c = default_calibration()
        h = c.stable_hash()
        assert len(h) == 64
        int(h, 16)  # parses as hex


# ──────────────────────────────────────────────────────────────────────
# Extract from audit
# ──────────────────────────────────────────────────────────────────────


class TestExtract:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        result = extract_calibration_from_audit(tmp_path / "no.jsonl")
        assert result is None

    def test_below_min_samples_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "small.jsonl"
        # Only 3 records — below MIN_SAMPLES_FOR_CALIBRATION.
        lines = [
            {"aid": f"s{i}", "explain": {
                "m13_score": 0.4,
                "session_drift": {"topic_drift": 0.1},
            }}
            for i in range(3)
        ]
        path.write_text("\n".join(json.dumps(item) for item in lines) + "\n")
        assert extract_calibration_from_audit(path) is None

    def test_extracts_percentiles_from_synthetic_audit(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "audit.jsonl"
        # 20 records spanning a known distribution.
        m13_values = [0.1 + 0.04 * i for i in range(20)]  # 0.10 .. 0.86
        drift_values = [0.0 + 0.05 * i for i in range(20)]  # 0.0 .. 0.95
        lines = []
        for i in range(20):
            lines.append(json.dumps({
                "aid": f"sess-{i % 7}",          # 7 distinct sessions
                "explain": {
                    "m13_score": m13_values[i],
                    "session_drift": {"topic_drift": drift_values[i]},
                },
            }))
        path.write_text("\n".join(lines) + "\n")

        c = extract_calibration_from_audit(path)
        assert c is not None
        assert c.is_usable()
        assert c.n_sessions == 7
        assert c.m13_score_n == 20
        assert c.topic_drift_n == 20
        # p10 of m13_values=[0.10..0.86] is around 0.17.
        assert 0.13 <= c.m13_score_p10 <= 0.22
        # p95 of drift_values=[0.0..0.95] is around 0.90.
        assert 0.85 <= c.topic_drift_p95 <= 0.95

    def test_skips_records_without_signals(self, tmp_path: Path) -> None:
        """Records without ``m13_score`` or without ``session_drift``
        must be silently skipped — they don't contribute to either
        percentile."""
        path = tmp_path / "audit.jsonl"
        lines = [
            json.dumps({"aid": "x", "explain": {}}),  # no signals
            json.dumps({"aid": "x", "explain": {"m13_score": "garbage"}}),
        ]
        for i in range(10):
            lines.append(json.dumps({
                "aid": f"s{i}", "explain": {
                    "m13_score": 0.4 + 0.01 * i,
                    "session_drift": {"topic_drift": 0.1 + 0.01 * i},
                },
            }))
        path.write_text("\n".join(lines) + "\n")
        c = extract_calibration_from_audit(path)
        assert c is not None
        assert c.m13_score_n == 10  # garbage / missing not counted

    def test_handles_corrupt_lines(self, tmp_path: Path) -> None:
        """A blank or non-JSON line in the audit must not abort the
        walk (matches the never-crash contract)."""
        path = tmp_path / "audit.jsonl"
        good = "\n".join(
            json.dumps({"aid": f"s{i}", "explain": {
                "m13_score": 0.3 + 0.01 * i,
                "session_drift": {"topic_drift": 0.1 + 0.01 * i},
            }}) for i in range(8)
        )
        # Inject blank + corrupt lines.
        path.write_text(
            good + "\n\n!! not json !!\n"
            + json.dumps({"aid": "s9", "explain": {
                "m13_score": 0.4,
                "session_drift": {"topic_drift": 0.2},
            }}) + "\n"
        )
        c = extract_calibration_from_audit(path)
        assert c is not None
        assert c.m13_score_n == 9


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


class TestJsonRoundTrip:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "cal.json"
        save_calibration(default_calibration(), path)
        loaded = load_calibration(path)
        assert loaded.is_usable()
        assert loaded.m13_score_p10 == default_calibration().m13_score_p10

    def test_load_or_default_on_missing(self, tmp_path: Path) -> None:
        c = load_calibration_or_default(tmp_path / "nope.json")
        assert c.extracted_from == "synthetic-default"

    def test_to_from_dict_preserves_values(self) -> None:
        original = default_calibration()
        d = calibration_to_dict(original)
        restored = calibration_from_dict(d)
        assert restored.m13_score_p10 == original.m13_score_p10
        assert restored.topic_drift_p95 == original.topic_drift_p95
        assert restored.n_sessions == original.n_sessions

    def test_shipped_default_file_exists(self) -> None:
        # ``models/advisor_calibration_v1.json`` is part of the repo.
        path = default_calibration_path()
        assert path.is_file(), f"shipped default missing at {path}"
        c = load_calibration(path)
        assert c.is_usable()


# ──────────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────────


class TestRender:
    def test_render_includes_threshold_markers(self) -> None:
        out = render_calibration(default_calibration())
        assert "AdvisorCalibration v1" in out
        assert "← gate trigger" in out


# ──────────────────────────────────────────────────────────────────────
# Hook gate integration — ensures signals 6 & 7 fire correctly
# ──────────────────────────────────────────────────────────────────────


class TestGateIntegration:
    """Direct test of ``_should_invoke_advisor`` consulting the
    calibration. Avoids re-running the full firewall."""

    def test_low_m13_fires_signal_6(self) -> None:
        import sys as _sys
        from pathlib import Path as _Pth

        _sys.path.insert(0, str(_Pth(__file__).resolve().parents[2] / "tools"))
        import aegis_local_hook as h

        # Reset the calibration singleton to pick up the shipped
        # default (the singleton is per-process, so a previous test
        # may have set it to ``False``).
        h._CALIBRATION_SINGLETON = None

        class V:
            decision = "ALLOW"
            reason = ""
            step_traces: dict[str, str] = {}

        invoked, reason = h._should_invoke_advisor(
            V(),
            {"m13_score": 0.05, "step_traces": {}},
        )
        assert invoked is True
        assert "M13 score" in reason
        assert "p10" in reason

    def test_high_m13_does_not_fire(self) -> None:
        import sys as _sys
        from pathlib import Path as _Pth

        _sys.path.insert(0, str(_Pth(__file__).resolve().parents[2] / "tools"))
        import aegis_local_hook as h
        h._CALIBRATION_SINGLETON = None

        class V:
            decision = "ALLOW"
            reason = ""
            step_traces: dict[str, str] = {}

        invoked, _ = h._should_invoke_advisor(
            V(),
            {"m13_score": 0.40, "step_traces": {}},
        )
        assert invoked is False

    def test_high_drift_fires_signal_7(self) -> None:
        import sys as _sys
        from pathlib import Path as _Pth

        _sys.path.insert(0, str(_Pth(__file__).resolve().parents[2] / "tools"))
        import aegis_local_hook as h
        h._CALIBRATION_SINGLETON = None

        class V:
            decision = "ALLOW"
            reason = ""
            step_traces: dict[str, str] = {}

        invoked, reason = h._should_invoke_advisor(
            V(),
            {
                "m13_score": 0.40,
                "session_drift": {"topic_drift": 0.85},
                "step_traces": {},
            },
        )
        assert invoked is True
        assert "session drift" in reason
