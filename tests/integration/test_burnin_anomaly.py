"""Tests for ``aegis.burnin.anomaly`` — distribution baseline +
anomaly tagging (PR-ε).

Verifies:

* MetricStats z-score behaviour incl. degenerate cases
* extract_baseline_from_audit walks Stop retrospectives correctly
* compute_anomalies surfaces tags only when |z| ≥ INFO_THRESHOLD
* severity bins (info / warning / alert) at 1σ / 2σ / 3σ
* JSON round-trip stability
* serialize_temporal integration appends ANOMALIES section
* default_baseline produces a usable baseline
* unusable baseline (n < MIN_SAMPLES) returns empty tag list
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from aegis.atv.temporal import TemporalContext, serialize_temporal
from aegis.burnin.anomaly import (
    ALERT_THRESHOLD,
    INFO_THRESHOLD,
    MIN_SAMPLES_FOR_BASELINE,
    WARNING_THRESHOLD,
    AnomalyTag,
    BurnInBaseline,
    MetricStats,
    baseline_from_dict,
    baseline_to_dict,
    compute_anomalies,
    default_baseline,
    extract_baseline_from_audit,
    load_baseline,
    load_baseline_or_default,
    render_anomalies,
    save_baseline,
)

# ──────────────────────────────────────────────────────────────────────
# MetricStats primitives
# ──────────────────────────────────────────────────────────────────────


class TestMetricStats:
    def test_zero_std_returns_zero_z_score(self) -> None:
        s = MetricStats(mean=5.0, std=0.0, p50=5.0, p95=5.0, p99=5.0,
                        n_samples=10)
        assert s.z_score(100.0) == 0.0  # never inf

    def test_too_few_samples_returns_zero(self) -> None:
        s = MetricStats(mean=5.0, std=1.0, p50=5.0, p95=6.0, p99=6.5,
                        n_samples=1)
        assert s.z_score(10.0) == 0.0

    def test_z_score_correct(self) -> None:
        s = MetricStats(mean=10.0, std=2.0, p50=10.0, p95=14.0, p99=15.0,
                        n_samples=50)
        assert s.z_score(14.0) == pytest.approx(2.0)
        assert s.z_score(8.0) == pytest.approx(-1.0)
        assert s.z_score(10.0) == pytest.approx(0.0)


# ──────────────────────────────────────────────────────────────────────
# extract_baseline_from_audit
# ──────────────────────────────────────────────────────────────────────


def _write_stop_record(
    fh, *, aid: str, ts_ns: int,
    cache_hit_rate: float = 0.5,
    backtrack_ratio: float = 0.0,
    redundancy_ratio: float = 0.0,
    error_rate: float = 0.0,
    input_tokens_total: float = 50_000,
    cumulative_billed_dollars: float = 1.0,
) -> None:
    fh.write(json.dumps({
        "ts_ns": ts_ns, "aid": aid, "hook": "Stop",
        "explain": {
            "session_retrospective": {
                "aid": aid,
                "cache_hit_rate": cache_hit_rate,
                "backtrack_ratio": backtrack_ratio,
                "redundancy_ratio": redundancy_ratio,
                "error_rate": error_rate,
                "input_tokens_total": input_tokens_total,
                "cumulative_billed_dollars": cumulative_billed_dollars,
            },
        },
    }) + "\n")


class TestExtractor:
    def test_empty_audit_yields_zero_sessions(
        self, tmp_path: Path,
    ) -> None:
        bl = extract_baseline_from_audit(tmp_path / "missing.jsonl")
        assert bl.n_sessions == 0
        assert not bl.is_usable()

    def test_extracts_session_count(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        with path.open("w") as fh:
            for i in range(8):
                _write_stop_record(
                    fh, aid=f"sess-{i}", ts_ns=i,
                    cache_hit_rate=0.5 + 0.03 * i,
                )
        bl = extract_baseline_from_audit(path)
        assert bl.n_sessions == 8
        assert bl.is_usable()
        assert bl.session_cache_hit_rate.n_samples == 8
        # Mean roughly 0.5 to 0.7 → ~0.605 with above values.
        assert 0.5 <= bl.session_cache_hit_rate.mean <= 0.7

    def test_skips_non_stop_records(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        with path.open("w") as fh:
            for i in range(5):
                _write_stop_record(fh, aid=f"a{i}", ts_ns=i)
            # Non-Stop records mixed in:
            fh.write(json.dumps({
                "ts_ns": 100, "aid": "x", "hook": "PostToolUse",
                "tool": "Bash", "status": "success",
            }) + "\n")
            fh.write(json.dumps({
                "ts_ns": 101, "aid": "x", "decision": "ALLOW",
                "tool": "Read",
            }) + "\n")
        bl = extract_baseline_from_audit(path)
        assert bl.n_sessions == 5

    def test_unusable_baseline_below_min_samples(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "audit.jsonl"
        with path.open("w") as fh:
            for i in range(MIN_SAMPLES_FOR_BASELINE - 1):
                _write_stop_record(fh, aid=f"s{i}", ts_ns=i)
        bl = extract_baseline_from_audit(path)
        assert not bl.is_usable()


# ──────────────────────────────────────────────────────────────────────
# compute_anomalies
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _FakeCtx:
    """Minimal stand-in for TemporalContext (only the fields the
    comparator reads)."""

    history: list[Any] = field(default_factory=lambda: ["t"] * 5)
    token_velocity_per_turn: float = 0.0
    cache_hit_rate_max_drop_pp: float = 0.0
    n_backtracks: int = 0
    n_redundant: int = 0
    n_errors: int = 0


class TestComputeAnomalies:
    def test_unusable_baseline_returns_empty(self) -> None:
        bl = BurnInBaseline(
            version=1, n_sessions=2, extracted_at_ns=0, extracted_from="x",
            session_cache_hit_rate=MetricStats(0.5, 0.1, 0.5, 0.7, 0.8, 2),
            session_backtrack_ratio=MetricStats(0, 0, 0, 0, 0, 2),
            session_redundancy_ratio=MetricStats(0, 0, 0, 0, 0, 2),
            session_error_rate=MetricStats(0, 0, 0, 0, 0, 2),
            session_cumulative_tokens=MetricStats(0, 0, 0, 0, 0, 2),
            session_cumulative_billed_dollars=MetricStats(0, 0, 0, 0, 0, 2),
            window_token_velocity_per_turn=MetricStats(0, 0, 0, 0, 0, 2),
            window_cache_hit_rate_max_drop_pp=MetricStats(0, 0, 0, 0, 0, 2),
            window_n_backtracks=MetricStats(0, 0, 0, 0, 0, 2),
            window_n_redundant=MetricStats(0, 0, 0, 0, 0, 2),
            window_n_errors=MetricStats(0, 0, 0, 0, 0, 2),
        )
        ctx = _FakeCtx(token_velocity_per_turn=999_999)
        tags = compute_anomalies(temporal_ctx=ctx, baseline=bl)
        assert tags == []

    def test_below_threshold_no_tags(self) -> None:
        bl = default_baseline()
        # Within 0.5σ on every metric.
        ctx = _FakeCtx(
            token_velocity_per_turn=1_000,   # μ=800, σ=600 → z≈0.33
            cache_hit_rate_max_drop_pp=8.0,  # μ=8 → z=0
            n_backtracks=0,
            n_redundant=0,
            n_errors=0,
        )
        tags = compute_anomalies(temporal_ctx=ctx, baseline=bl)
        assert tags == []

    def test_alert_when_3sigma_exceeded(self) -> None:
        bl = default_baseline()
        # cache_hit_rate_max_drop_pp baseline μ=8, σ=12 → 3σ at 44
        ctx = _FakeCtx(cache_hit_rate_max_drop_pp=80.0)
        tags = compute_anomalies(temporal_ctx=ctx, baseline=bl)
        cache_tags = [
            t for t in tags if "cache_hit_rate" in t.metric
        ]
        assert cache_tags
        assert cache_tags[0].severity == "alert"

    def test_warning_at_2sigma(self) -> None:
        bl = default_baseline()
        # token_velocity μ=800, σ=600 → 2σ at 2000
        ctx = _FakeCtx(token_velocity_per_turn=2_000)
        tags = compute_anomalies(temporal_ctx=ctx, baseline=bl)
        velocity_tags = [
            t for t in tags if "token_velocity" in t.metric
        ]
        assert velocity_tags
        assert velocity_tags[0].severity in {"warning", "alert"}
        assert abs(velocity_tags[0].z_score) >= WARNING_THRESHOLD

    def test_session_retrospective_drives_session_tags(self) -> None:
        bl = default_baseline()
        ctx = _FakeCtx()
        retro = {
            "cache_hit_rate": 0.95,         # μ=0.40, σ=0.30 → z≈1.83
            "backtrack_ratio": 0.40,        # μ=0.05, σ=0.05 → z=7
            "input_tokens_total": 1_000_000,
            "cumulative_billed_dollars": 50.0,
        }
        tags = compute_anomalies(
            temporal_ctx=ctx, baseline=bl, session_retrospective=retro,
        )
        names = {t.metric for t in tags}
        assert "session_backtrack_ratio" in names
        # 7σ → alert
        bt = next(t for t in tags if t.metric == "session_backtrack_ratio")
        assert bt.severity == "alert"

    def test_tags_sorted_alert_first(self) -> None:
        bl = default_baseline()
        # Mix of severities: 7σ backtrack, 3σ velocity, 1.5σ errors
        ctx = _FakeCtx(
            n_backtracks=2,                   # 6σ above μ=0.1 σ=0.3
            token_velocity_per_turn=3_000,    # 3.7σ
            n_errors=1,                       # 2σ
        )
        tags = compute_anomalies(temporal_ctx=ctx, baseline=bl)
        # alerts come first, then warnings, then info.
        sevs = [t.severity for t in tags]
        sev_order = {"alert": 0, "warning": 1, "info": 2}
        ranks = [sev_order[s] for s in sevs]
        assert ranks == sorted(ranks)


# ──────────────────────────────────────────────────────────────────────
# JSON round-trip
# ──────────────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        bl = default_baseline()
        path = tmp_path / "baseline.json"
        save_baseline(bl, path)
        loaded = load_baseline(path)
        assert loaded.n_sessions == bl.n_sessions
        assert loaded.session_cache_hit_rate.mean == \
            bl.session_cache_hit_rate.mean

    def test_baseline_to_dict_serialisable(self) -> None:
        d = baseline_to_dict(default_baseline())
        # Must JSON-serialise.
        json.dumps(d)

    def test_baseline_from_dict_handles_missing_keys(self) -> None:
        # An older baseline file that's missing some metrics —
        # the loader fills with empty stats rather than failing.
        d = {"version": 1, "n_sessions": 50}
        bl = baseline_from_dict(d)
        assert bl.n_sessions == 50
        # Missing metrics get n_samples=0 stats.
        assert bl.session_cache_hit_rate.n_samples == 0


# ──────────────────────────────────────────────────────────────────────
# Default loader
# ──────────────────────────────────────────────────────────────────────


class TestDefaultLoader:
    def test_load_or_default_returns_none_when_missing(
        self, tmp_path: Path,
    ) -> None:
        # Point the env var at a non-existent file.
        absent = tmp_path / "absent.json"
        bl = load_baseline_or_default(absent)
        assert bl is None

    def test_load_or_default_returns_loaded(self, tmp_path: Path) -> None:
        path = tmp_path / "bl.json"
        save_baseline(default_baseline(), path)
        bl = load_baseline_or_default(path)
        assert bl is not None
        assert bl.n_sessions == 100


# ──────────────────────────────────────────────────────────────────────
# default_baseline
# ──────────────────────────────────────────────────────────────────────


class TestDefaultBaseline:
    def test_default_is_usable(self) -> None:
        bl = default_baseline()
        assert bl.is_usable()
        assert bl.n_sessions >= MIN_SAMPLES_FOR_BASELINE

    def test_default_window_stats_populated(self) -> None:
        # The shipped default MUST include window stats — without
        # them no temporal-window anomalies fire.
        bl = default_baseline()
        assert bl.window_token_velocity_per_turn.n_samples > 0
        assert bl.window_n_backtracks.n_samples > 0


# ──────────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────────


class TestRenderer:
    def test_empty_tags_yields_empty_string(self) -> None:
        assert render_anomalies([]) == ""

    def test_renders_each_tag_one_line(self) -> None:
        tags = [
            AnomalyTag(
                metric="m1", severity="alert",
                observed=10.0, baseline_mean=1.0, baseline_std=1.0,
                z_score=9.0, description="m1 is 9σ above baseline",
            ),
        ]
        rendered = render_anomalies(tags)
        assert "ANOMALIES vs BURN-IN" in rendered
        assert "alert" in rendered
        assert "9σ above" in rendered

    def test_severity_glyphs_present(self) -> None:
        tags = [
            AnomalyTag(
                metric="m1", severity="alert",
                observed=10.0, baseline_mean=1.0, baseline_std=1.0,
                z_score=9.0, description="x",
            ),
            AnomalyTag(
                metric="m2", severity="warning",
                observed=5.0, baseline_mean=1.0, baseline_std=1.0,
                z_score=4.0, description="y",
            ),
            AnomalyTag(
                metric="m3", severity="info",
                observed=2.0, baseline_mean=1.0, baseline_std=1.0,
                z_score=1.5, description="z",
            ),
        ]
        rendered = render_anomalies(tags)
        # Alert sigil 🚨, warning ⚠, info ·
        assert "🚨" in rendered
        assert "⚠" in rendered


# ──────────────────────────────────────────────────────────────────────
# serialize_temporal integration
# ──────────────────────────────────────────────────────────────────────


def _mk_temporal_ctx(
    *, token_velocity: float = 0.0,
    n_backtracks: int = 0, n_redundant: int = 0, n_errors: int = 0,
    cache_drop_pp: float = 0.0,
) -> TemporalContext:
    from aegis.atv.temporal import ATVSnapshot

    snap = ATVSnapshot(
        turn_index_rel=0, ts_ns=0, tool_name="Read", args_excerpt="",
        decision="ALLOW", outcome="success",
    )
    return TemporalContext(
        history=(snap,) * 5,
        window_size=5,
        cumulative_token_trajectory=(0, 0, 0, 0, 0),
        cache_hit_rate_trajectory=(0.0, 0.0, 0.0, 0.0, 0.0),
        n_backtracks=n_backtracks, n_redundant=n_redundant,
        n_errors=n_errors, n_failures=0,
        cache_hit_rate_max_drop_pp=cache_drop_pp,
        token_velocity_per_turn=token_velocity,
        is_progress_stalled=False,
        distinct_tools_in_window=("Read",),
    )


class TestNarrativeIntegration:
    def test_no_baseline_no_anomalies_section(self) -> None:
        ctx = _mk_temporal_ctx(token_velocity=999_999)
        text = serialize_temporal(ctx)
        assert "ANOMALIES" not in text

    def test_baseline_attached_section_appears(self) -> None:
        ctx = _mk_temporal_ctx(token_velocity=5_000)
        bl = default_baseline()
        text = serialize_temporal(ctx, baseline=bl)
        assert "ANOMALIES vs BURN-IN" in text
        assert "token_velocity" in text

    def test_normal_ctx_under_baseline_no_section(self) -> None:
        # All within 0.5σ → no tags → no section.
        ctx = _mk_temporal_ctx(token_velocity=800)
        bl = default_baseline()
        text = serialize_temporal(ctx, baseline=bl)
        assert "ANOMALIES vs BURN-IN" not in text


# ──────────────────────────────────────────────────────────────────────
# Threshold constants exposed
# ──────────────────────────────────────────────────────────────────────


class TestThresholds:
    def test_threshold_constants_correct_order(self) -> None:
        assert INFO_THRESHOLD < WARNING_THRESHOLD < ALERT_THRESHOLD

    def test_min_samples_const_at_least_2(self) -> None:
        # Below 2 we can't compute meaningful std.
        assert MIN_SAMPLES_FOR_BASELINE >= 2
