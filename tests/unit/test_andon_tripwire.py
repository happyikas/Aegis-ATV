"""Tests for v0.5.24 — andon tripwire.

Three layers:

1. State persistence — round-trip read/write + defensive parsing.
2. Counter semantics — increment on bypass, reset on tripwire,
   skip on declined / blocked.
3. Runtime integration — `apply_autonomy_bypass` fires the
   tripwire after N consecutive bypasses regardless of trust.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aegis.autonomy.andon import (
    DEFAULT_ANDON_THRESHOLD,
    AndonState,
    andon_threshold_from_env,
    load_state,
    record_andon,
    record_bypass,
    reset_counter,
    should_fire_andon,
)
from aegis.autonomy.learner import TrustedPattern
from aegis.autonomy.runtime import (
    STEP_TRACE_ANDON_KEY,
    STEP_TRACE_KEY,
    apply_autonomy_bypass,
)
from aegis.schema import Verdict

# ──────────────────────────────────────────────────────────────────
# 1. State persistence
# ──────────────────────────────────────────────────────────────────


class TestStatePersistence:
    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        s = load_state(tmp_path / "absent.json")
        assert s.consecutive_bypasses == 0
        assert s.last_bypass_ns == 0

    def test_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "andon.json"
        state = AndonState(consecutive_bypasses=5, last_bypass_ns=1)
        record_bypass(state, path=target, now_ns=999)
        loaded = load_state(target)
        # record_bypass increments by 1.
        assert loaded.consecutive_bypasses == 6
        assert loaded.last_bypass_ns == 999

    def test_malformed_file_returns_zero(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.json"
        target.write_text("not json", encoding="utf-8")
        assert load_state(target).consecutive_bypasses == 0

    def test_reset(self, tmp_path: Path) -> None:
        target = tmp_path / "andon.json"
        record_bypass(AndonState(consecutive_bypasses=10), path=target)
        assert load_state(target).consecutive_bypasses == 11
        reset_counter(target)
        assert load_state(target).consecutive_bypasses == 0


# ──────────────────────────────────────────────────────────────────
# 2. Env threshold
# ──────────────────────────────────────────────────────────────────


class TestEnvThreshold:
    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AEGIS_AUTONOMY_ANDON_THRESHOLD", raising=False)
        assert andon_threshold_from_env() == DEFAULT_ANDON_THRESHOLD

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AEGIS_AUTONOMY_ANDON_THRESHOLD", "5")
        assert andon_threshold_from_env() == 5

    def test_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AEGIS_AUTONOMY_ANDON_THRESHOLD", "0")
        assert andon_threshold_from_env() == 0

    def test_garbage_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AEGIS_AUTONOMY_ANDON_THRESHOLD", "not-a-number")
        assert andon_threshold_from_env() == DEFAULT_ANDON_THRESHOLD

    def test_negative_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AEGIS_AUTONOMY_ANDON_THRESHOLD", "-1")
        assert andon_threshold_from_env() == DEFAULT_ANDON_THRESHOLD


# ──────────────────────────────────────────────────────────────────
# 3. Decision API
# ──────────────────────────────────────────────────────────────────


class TestShouldFireAndon:
    def test_under_threshold(self, tmp_path: Path) -> None:
        target = tmp_path / "andon.json"
        record_bypass(AndonState(consecutive_bypasses=4), path=target)
        # Now at 5.
        fire, state = should_fire_andon(threshold=10, path=target)
        assert fire is False

    def test_at_threshold_fires(self, tmp_path: Path) -> None:
        target = tmp_path / "andon.json"
        # State has 10 consecutive — at threshold 10 should fire.
        record_bypass(AndonState(consecutive_bypasses=9), path=target)
        fire, _ = should_fire_andon(threshold=10, path=target)
        assert fire is True

    def test_zero_threshold_disables(self, tmp_path: Path) -> None:
        target = tmp_path / "andon.json"
        record_bypass(AndonState(consecutive_bypasses=999), path=target)
        fire, _ = should_fire_andon(threshold=0, path=target)
        assert fire is False


class TestRecordSemantics:
    def test_record_bypass_increments(self, tmp_path: Path) -> None:
        target = tmp_path / "andon.json"
        new = record_bypass(AndonState(consecutive_bypasses=3), path=target)
        assert new.consecutive_bypasses == 4
        assert load_state(target).consecutive_bypasses == 4

    def test_record_andon_resets(self, tmp_path: Path) -> None:
        target = tmp_path / "andon.json"
        new = record_andon(
            AndonState(consecutive_bypasses=20),
            path=target,
            now_ns=12345,
        )
        assert new.consecutive_bypasses == 0
        assert new.last_andon_ns == 12345


# ──────────────────────────────────────────────────────────────────
# 4. Runtime integration
# ──────────────────────────────────────────────────────────────────


def _trusted() -> TrustedPattern:
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
    )


def _verdict(atv_id: str = "atv-test") -> Verdict:
    return Verdict(
        decision="REQUIRE_APPROVAL",  # type: ignore[arg-type]
        reason="same Bash call repeated 3 times this session",
        atv_id=atv_id,
        signature="sig",
        confidence=0.5,
        step_traces={},
        step_timings_us={},
    )


class TestAndonRuntimeIntegration:
    def test_andon_fires_after_threshold(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
        monkeypatch.setenv(
            "AEGIS_AUTONOMY_ANDON_STATE", str(tmp_path / "andon.json"),
        )
        monkeypatch.setenv("AEGIS_AUTONOMY_ANDON_THRESHOLD", "3")
        trusted = _trusted()
        # Pre-populate the counter to be at threshold.
        record_bypass(
            AndonState(consecutive_bypasses=2),
            path=tmp_path / "andon.json",
        )
        v = _verdict()
        new_v, av = apply_autonomy_bypass(
            v,
            tool_name="Bash",
            reason=v.reason,
            trust_table={trusted.key: trusted},
            epsilon=0.0,
        )
        # Decision stays REQUIRE_APPROVAL; andon stamp present.
        assert new_v.decision == "REQUIRE_APPROVAL"
        assert STEP_TRACE_ANDON_KEY in new_v.step_traces
        assert "andon_tripwire" in av.outlier_signals
        # Counter resets after andon fires.
        assert load_state(tmp_path / "andon.json").consecutive_bypasses == 0

    def test_under_threshold_bypasses_and_increments(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
        monkeypatch.setenv(
            "AEGIS_AUTONOMY_ANDON_STATE", str(tmp_path / "andon.json"),
        )
        monkeypatch.setenv("AEGIS_AUTONOMY_ANDON_THRESHOLD", "10")
        trusted = _trusted()
        v = _verdict()
        new_v, av = apply_autonomy_bypass(
            v,
            tool_name="Bash",
            reason=v.reason,
            trust_table={trusted.key: trusted},
            epsilon=0.0,
        )
        assert new_v.decision == "ALLOW"
        assert STEP_TRACE_KEY in new_v.step_traces
        assert STEP_TRACE_ANDON_KEY not in new_v.step_traces
        # Counter incremented.
        assert load_state(tmp_path / "andon.json").consecutive_bypasses == 1

    def test_disabled_andon_does_not_fire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
        monkeypatch.setenv("AEGIS_AUTONOMY_ANDON_THRESHOLD", "0")  # disable
        monkeypatch.setenv(
            "AEGIS_AUTONOMY_ANDON_STATE", str(tmp_path / "andon.json"),
        )
        trusted = _trusted()
        # Pre-populate with high counter — would normally trip.
        record_bypass(
            AndonState(consecutive_bypasses=100),
            path=tmp_path / "andon.json",
        )
        v = _verdict()
        new_v, av = apply_autonomy_bypass(
            v,
            tool_name="Bash",
            reason=v.reason,
            trust_table={trusted.key: trusted},
            epsilon=0.0,
        )
        assert new_v.decision == "ALLOW"
        assert STEP_TRACE_ANDON_KEY not in new_v.step_traces

    def test_declined_bypass_does_not_increment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When the bypass is declined for a non-andon reason (e.g.
        low trust score), the counter should not increment."""
        monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
        monkeypatch.setenv(
            "AEGIS_AUTONOMY_ANDON_STATE", str(tmp_path / "andon.json"),
        )
        monkeypatch.setenv("AEGIS_AUTONOMY_ANDON_THRESHOLD", "10")
        low_trust = TrustedPattern(
            tool_name="Bash",
            reason_signature="loop:Bash",
            n_seen=5,
            n_followed_by_block=2,
            clean_rate=0.6,
            trust_score=0.4,  # below 0.85 threshold
            last_seen_ns=time.time_ns(),
        )
        v = _verdict()
        new_v, av = apply_autonomy_bypass(
            v,
            tool_name="Bash",
            reason=v.reason,
            trust_table={low_trust.key: low_trust},
            epsilon=0.0,
        )
        # Bypass declined — counter unchanged.
        assert new_v.decision == "REQUIRE_APPROVAL"
        assert load_state(tmp_path / "andon.json").consecutive_bypasses == 0
