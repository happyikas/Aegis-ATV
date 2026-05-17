"""Tests for v0.5.25 — session-prior calibration.

Three layers:

1. State persistence — round-trip + expiry + defensive parsing.
2. Label → threshold mapping for all three labels + fallback.
3. Runtime integration — `apply_autonomy_bypass` honours the
   per-label threshold and stamps the prior into step_traces.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aegis.autonomy.learner import TrustedPattern
from aegis.autonomy.runtime import STEP_TRACE_KEY, apply_autonomy_bypass
from aegis.autonomy.session_prior import (
    DEFAULT_TTL_HOURS,
    RISK_LABELS,
    end_session,
    load_session_prior,
    session_min_trust,
    start_session,
)
from aegis.schema import Verdict

# ──────────────────────────────────────────────────────────────────
# 1. State persistence
# ──────────────────────────────────────────────────────────────────


class TestStatePersistence:
    def test_missing_returns_default(self, tmp_path: Path) -> None:
        p = load_session_prior(tmp_path / "absent.json")
        assert p.is_default()
        assert p.label == "refactor"

    def test_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "sp.json"
        start_session("exploring", note="POC work", path=target)
        loaded = load_session_prior(target)
        assert loaded.label == "exploring"
        assert loaded.note == "POC work"
        assert loaded.is_default() is False

    def test_unknown_label_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            start_session("unknown-label", path=tmp_path / "sp.json")

    def test_all_known_labels_accepted(self, tmp_path: Path) -> None:
        for label in RISK_LABELS:
            target = tmp_path / f"{label}.json"
            start_session(label, path=target)
            loaded = load_session_prior(target)
            assert loaded.label == label

    def test_expiry_returns_default(self, tmp_path: Path) -> None:
        target = tmp_path / "sp.json"
        # Start with a 0.0001-hour TTL (~0.4s); sleep past it.
        start_session("exploring", ttl_hours=0.0001, path=target)
        time.sleep(0.5)
        loaded = load_session_prior(target)
        assert loaded.is_default()

    def test_zero_ttl_disables_expiry(self, tmp_path: Path) -> None:
        target = tmp_path / "sp.json"
        start_session("exploring", ttl_hours=0, path=target)
        loaded = load_session_prior(target)
        assert loaded.expires_at_ns == 0
        assert loaded.is_expired() is False

    def test_end_session(self, tmp_path: Path) -> None:
        target = tmp_path / "sp.json"
        start_session("exploring", path=target)
        end_session(target)
        assert load_session_prior(target).is_default()

    def test_malformed_returns_default(self, tmp_path: Path) -> None:
        target = tmp_path / "sp.json"
        target.write_text("not json", encoding="utf-8")
        assert load_session_prior(target).is_default()


# ──────────────────────────────────────────────────────────────────
# 2. Threshold mapping
# ──────────────────────────────────────────────────────────────────


class TestThresholdMapping:
    def test_default_uses_fallback(self, tmp_path: Path) -> None:
        threshold, prior = session_min_trust(
            0.85, path=tmp_path / "absent.json",
        )
        assert threshold == 0.85
        assert prior.is_default()

    def test_exploring_is_loose(self, tmp_path: Path) -> None:
        target = tmp_path / "sp.json"
        start_session("exploring", path=target)
        threshold, prior = session_min_trust(0.85, path=target)
        assert threshold == 0.70
        assert prior.label == "exploring"

    def test_refactor_matches_default(self, tmp_path: Path) -> None:
        target = tmp_path / "sp.json"
        start_session("refactor", path=target)
        threshold, _ = session_min_trust(0.85, path=target)
        assert threshold == 0.85

    def test_prod_deploy_is_strict(self, tmp_path: Path) -> None:
        target = tmp_path / "sp.json"
        start_session("prod-deploy", path=target)
        threshold, _ = session_min_trust(0.85, path=target)
        assert threshold == 0.95


class TestDefaults:
    def test_ttl_default(self) -> None:
        assert DEFAULT_TTL_HOURS == 8

    def test_labels_complete(self) -> None:
        assert set(RISK_LABELS) == {"exploring", "refactor", "prod-deploy"}


# ──────────────────────────────────────────────────────────────────
# 3. Runtime integration
# ──────────────────────────────────────────────────────────────────


def _trusted_at(score: float) -> TrustedPattern:
    return TrustedPattern(
        tool_name="Bash",
        reason_signature="loop:Bash",
        n_seen=200,
        n_followed_by_block=0,
        clean_rate=1.0,
        trust_score=score,
        last_seen_ns=time.time_ns(),
        n_effective=200.0,
    )


def _verdict() -> Verdict:
    return Verdict(
        decision="REQUIRE_APPROVAL",  # type: ignore[arg-type]
        reason="same Bash call repeated 3 times this session",
        atv_id="atv-sp",
        signature="sig",
        confidence=0.5,
        step_traces={},
        step_timings_us={},
    )


class TestSessionPriorRuntime:
    def test_no_prior_uses_default_threshold(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
        monkeypatch.setenv(
            "AEGIS_AUTONOMY_SESSION_PRIOR",
            str(tmp_path / "absent.json"),
        )
        # Trust score 0.80: below default 0.85 → refused.
        trusted = _trusted_at(0.80)
        v = _verdict()
        new_v, _ = apply_autonomy_bypass(
            v,
            tool_name="Bash",
            reason=v.reason,
            trust_table={trusted.key: trusted},
            epsilon=0.0,
        )
        assert new_v.decision == "REQUIRE_APPROVAL"

    def test_exploring_lowers_threshold(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "sp.json"
        start_session("exploring", path=target)
        monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
        monkeypatch.setenv("AEGIS_AUTONOMY_SESSION_PRIOR", str(target))
        # 0.75 is BELOW default (0.85) but ABOVE exploring (0.70).
        trusted = _trusted_at(0.75)
        v = _verdict()
        new_v, _ = apply_autonomy_bypass(
            v,
            tool_name="Bash",
            reason=v.reason,
            trust_table={trusted.key: trusted},
            epsilon=0.0,
        )
        assert new_v.decision == "ALLOW"
        assert (
            "aegis.autonomy.step331.session_prior" in new_v.step_traces
        )
        assert "exploring" in new_v.step_traces[
            "aegis.autonomy.step331.session_prior"
        ]

    def test_prod_deploy_blocks_normal_bypass(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "sp.json"
        start_session("prod-deploy", path=target)
        monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
        monkeypatch.setenv("AEGIS_AUTONOMY_SESSION_PRIOR", str(target))
        # 0.90 normally clears default but BELOW prod-deploy (0.95).
        trusted = _trusted_at(0.90)
        v = _verdict()
        new_v, _ = apply_autonomy_bypass(
            v,
            tool_name="Bash",
            reason=v.reason,
            trust_table={trusted.key: trusted},
            epsilon=0.0,
        )
        assert new_v.decision == "REQUIRE_APPROVAL"

    def test_refactor_matches_baseline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "sp.json"
        start_session("refactor", path=target)
        monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
        monkeypatch.setenv("AEGIS_AUTONOMY_SESSION_PRIOR", str(target))
        # 0.90 clears default 0.85 → bypass engages.
        trusted = _trusted_at(0.90)
        v = _verdict()
        new_v, _ = apply_autonomy_bypass(
            v,
            tool_name="Bash",
            reason=v.reason,
            trust_table={trusted.key: trusted},
            epsilon=0.0,
        )
        assert new_v.decision == "ALLOW"
        assert STEP_TRACE_KEY in new_v.step_traces
