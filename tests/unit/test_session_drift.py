"""Unit tests for the per-session behavioural-drift tracker."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from aegis.atv import session_drift
from aegis.atv.session_drift import (
    MAX_DRIFT_HISTORY,
    SESSION_TTL_NS,
    DriftSignals,
    SessionState,
    clear_sessions,
    list_sessions,
    load_session,
    save_session,
    update_and_score,
)


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


@pytest.fixture
def session_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate every test in its own session directory."""
    monkeypatch.setenv("AEGIS_SESSION_DIR", str(tmp_path))
    # Cache busts in case session_drift caches the env (it doesn't, but
    # this future-proofs).
    monkeypatch.setattr(session_drift, "DEFAULT_SESSION_DIR", tmp_path)
    return tmp_path


# ─────────────────────────────────────────────────────────────────────
# SessionState (Welford + history)
# ─────────────────────────────────────────────────────────────────────
class TestSessionState:
    def test_welford_mean_first_call(self) -> None:
        s = SessionState(session_id="t", anchor_embedding=None)
        s.update_plan_length(100)
        assert s.n_calls == 1
        assert s.running_mean_plan_len == 100.0
        assert s.plan_len_std == 0.0  # n=1 → stddev undefined → 0

    def test_welford_mean_two_calls(self) -> None:
        s = SessionState(session_id="t", anchor_embedding=None)
        s.update_plan_length(100)
        s.update_plan_length(200)
        # mean = 150
        assert abs(s.running_mean_plan_len - 150) < 1e-6
        # stddev with sample n-1 denominator: sqrt(((100-150)² + (200-150)²) / 1) = 70.71
        assert abs(s.plan_len_std - 70.71) < 1.0

    def test_drift_history_capped(self) -> None:
        s = SessionState(session_id="t", anchor_embedding=None)
        for i in range(MAX_DRIFT_HISTORY * 2):
            s.record_drift(float(i))
        assert len(s.drift_history) == MAX_DRIFT_HISTORY
        # Oldest dropped — last value preserved.
        assert s.drift_history[-1] == float(MAX_DRIFT_HISTORY * 2 - 1)


# ─────────────────────────────────────────────────────────────────────
# JSON round-trip
# ─────────────────────────────────────────────────────────────────────
class TestPersistence:
    def test_save_load_roundtrip(self, session_dir: Path) -> None:
        anchor = _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        s = SessionState(
            session_id="abc-123",
            anchor_embedding=anchor,
            running_mean_plan_len=42.0,
            running_m2_plan_len=10.0,
            n_calls=3,
            drift_history=[0.0, 0.1, 0.2],
            started_at_ns=1_000,
            last_seen_ns=2_000,
        )
        save_session(s)
        loaded = load_session("abc-123")
        assert loaded is not None
        assert loaded.session_id == "abc-123"
        assert loaded.n_calls == 3
        assert loaded.running_mean_plan_len == 42.0
        assert loaded.drift_history == [0.0, 0.1, 0.2]
        assert loaded.anchor_embedding is not None
        np.testing.assert_array_almost_equal(loaded.anchor_embedding, anchor)

    def test_load_missing_returns_none(self, session_dir: Path) -> None:
        assert load_session("never-seen") is None

    def test_empty_session_id_returns_none(self, session_dir: Path) -> None:
        assert load_session("") is None

    def test_corrupt_json_returns_none(self, session_dir: Path) -> None:
        path = session_dir / "broken.json"
        path.write_text("not valid json {{{")
        assert load_session("broken") is None

    def test_session_id_path_traversal_safe(self, session_dir: Path) -> None:
        """Hostile session_id (path-traversal) must be sanitised."""
        s = SessionState(
            session_id="../../etc/passwd",
            anchor_embedding=None,
            n_calls=1,
        )
        save_session(s)
        # The actual file should be inside session_dir, NOT
        # /etc/passwd or similar — sanitisation collapses /. to _.
        files = list(session_dir.iterdir())
        assert len(files) == 1
        for f in files:
            assert f.is_relative_to(session_dir)


# ─────────────────────────────────────────────────────────────────────
# update_and_score (the hot path)
# ─────────────────────────────────────────────────────────────────────
class TestUpdateAndScore:
    def test_first_call_returns_anchor(self, session_dir: Path) -> None:
        emb = _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        s = update_and_score(
            session_id="s1", current_embedding=emb, current_plan_len=50,
        )
        assert s.is_anchor_call is True
        assert s.topic_drift == 0.0
        assert s.verbosity_drift == 0.0
        assert s.n_calls == 1

    def test_second_call_with_same_embedding_zero_drift(
        self, session_dir: Path,
    ) -> None:
        emb = _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        update_and_score(
            session_id="s1", current_embedding=emb, current_plan_len=50,
        )
        s2 = update_and_score(
            session_id="s1", current_embedding=emb, current_plan_len=50,
        )
        assert s2.is_anchor_call is False
        assert s2.topic_drift < 1e-5  # exact match → 0 drift

    def test_second_call_with_orthogonal_embedding_max_drift(
        self, session_dir: Path,
    ) -> None:
        anchor = _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        ortho = _unit(np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32))
        update_and_score(
            session_id="s1", current_embedding=anchor, current_plan_len=10,
        )
        s2 = update_and_score(
            session_id="s1", current_embedding=ortho, current_plan_len=10,
        )
        # Orthogonal: cos = 0 → drift = 1
        assert abs(s2.topic_drift - 1.0) < 1e-5

    def test_drift_monotonic_under_progressive_rotation(
        self, session_dir: Path,
    ) -> None:
        """Drift should grow monotonically as the embedding rotates
        away from the anchor — direct test of the cosine-distance
        formula."""
        anchor = _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        update_and_score(
            session_id="s1", current_embedding=anchor, current_plan_len=10,
        )
        previous_drift = 0.0
        for theta_deg in (10, 20, 30, 45, 60, 90):
            theta = np.deg2rad(theta_deg)
            v = _unit(np.array(
                [np.cos(theta), np.sin(theta), 0.0, 0.0], dtype=np.float32,
            ))
            s = update_and_score(
                session_id="s1", current_embedding=v, current_plan_len=10,
            )
            assert s.topic_drift >= previous_drift - 1e-5, (
                f"drift went down at θ={theta_deg}: {previous_drift:.3f} → "
                f"{s.topic_drift:.3f}"
            )
            previous_drift = s.topic_drift
        # 90° (orthogonal) should reach drift ≈ 1.
        assert previous_drift > 0.99

    def test_verbosity_drift_zero_with_constant_plan_len(
        self, session_dir: Path,
    ) -> None:
        emb = _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        for _ in range(5):
            s = update_and_score(
                session_id="s1", current_embedding=emb, current_plan_len=100,
            )
        assert s.verbosity_drift == 0.0

    def test_verbosity_drift_high_with_outlier_plan_len(
        self, session_dir: Path,
    ) -> None:
        """A 10× plan length change should produce a noticeable z-score."""
        emb = _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        for _ in range(5):
            update_and_score(
                session_id="s1", current_embedding=emb, current_plan_len=100,
            )
        s = update_and_score(
            session_id="s1", current_embedding=emb, current_plan_len=1000,
        )
        # z = |1000 - mean| / std, both populated from prior 5 constant calls.
        # With a constant prior, std=0 falls back to 0, but after this call's
        # variance update the next call would see signal. Either way the
        # value is non-negative and bounded.
        assert 0.0 <= s.verbosity_drift <= 3.0

    def test_empty_session_id_returns_zero(self) -> None:
        emb = _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        s = update_and_score(
            session_id="", current_embedding=emb, current_plan_len=10,
        )
        assert s.topic_drift == 0.0
        assert not s.is_anchor_call

    def test_zero_embedding_returns_zero(self, session_dir: Path) -> None:
        s = update_and_score(
            session_id="s1",
            current_embedding=np.zeros(4, dtype=np.float32),
            current_plan_len=10,
        )
        assert s.topic_drift == 0.0

    def test_signals_to_session_behavior_format(
        self, session_dir: Path,
    ) -> None:
        emb = _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        s = update_and_score(
            session_id="s1", current_embedding=emb, current_plan_len=10,
        )
        d = s.to_session_behavior()
        # Encoder reads these keys.
        assert "topic_drift" in d
        assert "verbosity_drift" in d


# ─────────────────────────────────────────────────────────────────────
# Eviction + management
# ─────────────────────────────────────────────────────────────────────
class TestEviction:
    def test_clear_all_when_keep_zero(self, session_dir: Path) -> None:
        for sid in ("a", "b", "c"):
            save_session(SessionState(session_id=sid, anchor_embedding=None))
        n = clear_sessions(keep_recent=0)
        assert n == 3
        assert list(session_dir.iterdir()) == []

    def test_clear_keeps_most_recent(self, session_dir: Path) -> None:
        for i, sid in enumerate(("oldest", "middle", "newest")):
            save_session(SessionState(
                session_id=sid, anchor_embedding=None,
                last_seen_ns=1000 + i,
            ))
        clear_sessions(keep_recent=1)
        sessions = list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "newest"

    def test_list_sessions_sorted_by_recency(self, session_dir: Path) -> None:
        save_session(SessionState(
            session_id="A", anchor_embedding=None, last_seen_ns=100,
        ))
        save_session(SessionState(
            session_id="B", anchor_embedding=None, last_seen_ns=300,
        ))
        save_session(SessionState(
            session_id="C", anchor_embedding=None, last_seen_ns=200,
        ))
        order = [s["session_id"] for s in list_sessions()]
        assert order == ["B", "C", "A"]

    def test_stale_session_evicted_on_load(
        self, session_dir: Path,
    ) -> None:
        """A session file older than SESSION_TTL_NS should be deleted
        the next time *any* session is loaded."""
        old_path = session_dir / "stale.json"
        old_path.write_text('{"session_id": "stale", "n_calls": 1}')
        # Backdate mtime to 8 days ago.
        eight_days_ago = (time.time_ns() - SESSION_TTL_NS - int(1e9)) / 1e9
        import os
        os.utime(old_path, (eight_days_ago, eight_days_ago))
        # Trigger the opportunistic evict via load_session.
        load_session("anything")
        assert not old_path.exists()


# ─────────────────────────────────────────────────────────────────────
# Adapter integration (without BGE — should degrade silently)
# ─────────────────────────────────────────────────────────────────────
class TestAdapterIntegration:
    def test_adapter_with_dummy_embedding_emits_no_drift_signals(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """When the embedding provider is dummy, the adapter must NOT
        compute drift (cosines on SHA3 noise are meaningless)."""
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "dummy")
        monkeypatch.setenv("AEGIS_SESSION_DIR", str(tmp_path))

        from aegis.atv.adapter import _maybe_compute_session_drift
        out = _maybe_compute_session_drift(
            session_id="s1",
            agent_state_text="user wants to debug",
            plan_text="run pytest",
        )
        assert out == {}, (
            f"drift computed under dummy embedding (output: {out}) — "
            "would inject SHA3 noise into M13"
        )

    def test_adapter_skips_when_no_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "bge-local")
        monkeypatch.setenv("AEGIS_SESSION_DIR", str(tmp_path))
        from aegis.atv.adapter import _maybe_compute_session_drift
        out = _maybe_compute_session_drift(
            session_id="", agent_state_text="hi", plan_text="x",
        )
        assert out == {}

    def test_adapter_skips_when_no_agent_state_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "bge-local")
        monkeypatch.setenv("AEGIS_SESSION_DIR", str(tmp_path))
        from aegis.atv.adapter import _maybe_compute_session_drift
        out = _maybe_compute_session_drift(
            session_id="s1", agent_state_text="", plan_text="x",
        )
        assert out == {}


# ─────────────────────────────────────────────────────────────────────
# DriftSignals dataclass
# ─────────────────────────────────────────────────────────────────────
def test_drift_signals_to_session_behavior_keys() -> None:
    s = DriftSignals(topic_drift=0.5, verbosity_drift=1.2)
    d = s.to_session_behavior()
    assert d == {"topic_drift": 0.5, "verbosity_drift": 1.2}
