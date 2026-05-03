"""Per-session behavioural-drift tracking via BGE embeddings.

Patent's ``session_behavioral_drift`` subfield (slice 1808–1823) is a
16-D feature vector with 6 named slots: ``persona_drift``,
``refusal_rate_change``, ``tone_shift``, ``system_prompt_adherence_trend``,
``topic_drift``, ``verbosity_drift``. Pre-PR-#25 the encoder filled the
slot from ``inp.session_behavior`` — but **nothing populated that
dict**. It was always zero, contributing no signal to M13.

This module fills two of the named slots (the ones that are reliably
computable on the Solo Free hot path with the BGE encoder PR #22
shipped):

* ``topic_drift`` = ``1 − cos(anchor, current)`` — semantic distance
  from the agent's session-start state to its current state. A
  benign session stays near the anchor (0.0–0.2); a slow-drift
  prompt-injection attack monotonically pushes this up.
* ``verbosity_drift`` = absolute z-score of current ``plan_text``
  length vs the session's running mean. Sudden plan-bloat is a
  weak but real attacker signal.

The other four slots stay 0 — they need transcript-level features
(``persona_drift``, ``tone_shift``) or hook-side counters
(``refusal_rate_change``, ``system_prompt_adherence_trend``) that
we collect later.

Storage
-------
Per-session JSON files at ``~/.aegis/sessions/<session_id>.json``.
Each file:

* ``anchor_embedding``   — 768-D float32 list (the first call's
  agent_state vector)
* ``running_mean_plan_len`` / ``running_var_plan_len`` — Welford
  online moments for verbosity_drift
* ``n_calls``             — count
* ``drift_history``       — last 32 cosine-distances to anchor
* ``started_at_ns`` / ``last_seen_ns``

JSON not SQLite: lighter, easier to debug, compatible with the rest
of ``~/.aegis/`` JSONL log surface. A typical 8-hour Claude Code day
emits ~50 sessions × ~6 KB each = 300 KB — trivial.

Eviction
--------
On every load, sessions older than 7 days are deleted. No background
job needed. Eviction is opportunistic: if the user never invokes the
firewall again, stale files just sit there.

Failure mode
------------
Disk write errors / parse errors / missing BGE embedding never block
a tool call. The encoder gets zeros (same as v0 behaviour) and the
firewall continues. ATV-side behaviour with this module *off* is
bit-identical to the pre-PR-#25 firewall.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

# Default session store location. Override with ``AEGIS_SESSION_DIR``.
DEFAULT_SESSION_DIR = Path.home() / ".aegis" / "sessions"

# Sessions older than this are evicted on next load. 7 days picks up
# the typical "weekend gap" without losing context for a multi-day
# refactor.
SESSION_TTL_NS = int(7 * 24 * 3600 * 1e9)

# Cap drift_history at 32 entries — enough to compute a trend without
# bloating per-session JSON. Older entries are dropped FIFO.
MAX_DRIFT_HISTORY = 32


@dataclass
class SessionState:
    """Persisted per-session behaviour anchor + running stats."""

    session_id: str
    anchor_embedding: np.ndarray | None
    running_mean_plan_len: float = 0.0
    running_m2_plan_len: float = 0.0    # Welford "M2" — used for variance
    n_calls: int = 0
    drift_history: list[float] = field(default_factory=list)
    started_at_ns: int = 0
    last_seen_ns: int = 0

    @property
    def has_anchor(self) -> bool:
        return self.anchor_embedding is not None

    @property
    def plan_len_std(self) -> float:
        if self.n_calls < 2:
            return 0.0
        return float(np.sqrt(self.running_m2_plan_len / (self.n_calls - 1)))

    def update_plan_length(self, plan_len: int) -> None:
        """Welford online update for mean / variance.

        Called once per tool call. Lets us compute z-score for
        verbosity_drift without storing every plan_text.
        """
        self.n_calls += 1
        delta = plan_len - self.running_mean_plan_len
        self.running_mean_plan_len += delta / self.n_calls
        delta2 = plan_len - self.running_mean_plan_len
        self.running_m2_plan_len += delta * delta2

    def record_drift(self, drift: float) -> None:
        self.drift_history.append(float(drift))
        if len(self.drift_history) > MAX_DRIFT_HISTORY:
            self.drift_history = self.drift_history[-MAX_DRIFT_HISTORY:]

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "anchor_embedding": (
                self.anchor_embedding.tolist()
                if self.anchor_embedding is not None
                else None
            ),
            "running_mean_plan_len": float(self.running_mean_plan_len),
            "running_m2_plan_len": float(self.running_m2_plan_len),
            "n_calls": self.n_calls,
            "drift_history": list(self.drift_history),
            "started_at_ns": int(self.started_at_ns),
            "last_seen_ns": int(self.last_seen_ns),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionState:
        anchor_raw = data.get("anchor_embedding")
        anchor = (
            np.asarray(anchor_raw, dtype=np.float32)
            if anchor_raw is not None
            else None
        )
        return cls(
            session_id=str(data.get("session_id", "")),
            anchor_embedding=anchor,
            running_mean_plan_len=float(data.get("running_mean_plan_len", 0.0)),
            running_m2_plan_len=float(data.get("running_m2_plan_len", 0.0)),
            n_calls=int(data.get("n_calls", 0)),
            drift_history=[float(x) for x in data.get("drift_history", [])],
            started_at_ns=int(data.get("started_at_ns", 0)),
            last_seen_ns=int(data.get("last_seen_ns", 0)),
        )


def session_dir() -> Path:
    raw = os.environ.get("AEGIS_SESSION_DIR", "").strip()
    return Path(raw) if raw else DEFAULT_SESSION_DIR


def _session_path(session_id: str) -> Path:
    # Sanitise: only allow [A-Za-z0-9_-]; everything else collapses to "_".
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in session_id)
    if not safe:
        safe = "default"
    return session_dir() / f"{safe[:120]}.json"


def _evict_stale(now_ns: int) -> None:
    """Delete session files past SESSION_TTL_NS. Best-effort, never raises."""
    d = session_dir()
    if not d.exists():
        return
    cutoff = now_ns - SESSION_TTL_NS
    try:
        for p in d.iterdir():
            if not p.is_file() or p.suffix != ".json":
                continue
            try:
                last = p.stat().st_mtime_ns
                if last < cutoff:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        return


def load_session(session_id: str) -> SessionState | None:
    """Read the session JSON if it exists, else None.

    Eviction runs opportunistically here — keeps stale state from
    accumulating without needing a separate cron / hook.
    """
    if not session_id:
        return None
    _evict_stale(time.time_ns())
    p = _session_path(session_id)
    if not p.exists():
        return None
    try:
        return SessionState.from_json(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def save_session(state: SessionState) -> None:
    """Write the session JSON atomically. Best-effort — never raises."""
    p = _session_path(state.session_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(state.to_json(), separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(p)
    except OSError:
        return


# ─────────────────────────────────────────────────────────────────────
# Drift computation (the actual feature extraction)
# ─────────────────────────────────────────────────────────────────────


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


@dataclass
class DriftSignals:
    """The signals we feed into ``inp.session_behavior``."""

    topic_drift: float = 0.0          # 1 − cos(anchor, current); [0, 2]
    verbosity_drift: float = 0.0      # |z-score| of current plan_len; [0, ~3]
    n_calls: int = 0                  # for downstream aggregators
    is_anchor_call: bool = False      # True iff this is the session's 1st call

    def to_session_behavior(self) -> dict[str, float]:
        """Render as the dict shape ``ATVInput.session_behavior`` expects."""
        return {
            "topic_drift":     self.topic_drift,
            "verbosity_drift": self.verbosity_drift,
        }


def update_and_score(
    *, session_id: str, current_embedding: np.ndarray,
    current_plan_len: int, persist: bool = True,
) -> DriftSignals:
    """Main entry point — load state, compute drift, update + persist.

    First call in a session: stores ``current_embedding`` as the anchor,
    returns ``DriftSignals(topic_drift=0, is_anchor_call=True)`` (nothing
    to drift from yet).

    Subsequent calls: cosine-distance to anchor → ``topic_drift``,
    Welford z-score of ``current_plan_len`` → ``verbosity_drift``.
    Persists updated state at the end.

    The function tolerates missing inputs (empty session_id, zero
    embedding) by returning an all-zero ``DriftSignals`` — the encoder
    on the receiving end then writes zeros to the slot, which is the
    pre-PR-#25 default. Never raises.
    """
    if not session_id:
        return DriftSignals()
    if current_embedding is None or current_embedding.size == 0:
        return DriftSignals()
    cur = np.asarray(current_embedding, dtype=np.float32).ravel()
    if not np.isfinite(cur).all() or float(np.linalg.norm(cur)) == 0:
        return DriftSignals()

    state = load_session(session_id)
    now = time.time_ns()
    is_anchor = False
    if state is None or not state.has_anchor:
        # First call — anchor the session.
        state = SessionState(
            session_id=session_id,
            anchor_embedding=cur.copy(),
            started_at_ns=now,
            last_seen_ns=now,
        )
        state.update_plan_length(int(current_plan_len))
        state.record_drift(0.0)
        is_anchor = True
        if persist:
            save_session(state)
        return DriftSignals(
            topic_drift=0.0, verbosity_drift=0.0,
            n_calls=state.n_calls, is_anchor_call=True,
        )

    # Returning call — compute drift signals.
    assert state.anchor_embedding is not None
    cos = _cosine(state.anchor_embedding, cur)
    # Map cosine ∈ [-1, 1] → drift ∈ [0, 2]; clip in case of float wobble.
    topic_drift = max(0.0, min(2.0, 1.0 - cos))

    # Verbosity z-score uses *pre-update* moments so the current call
    # doesn't bias its own "anomaly" measurement.
    plan_len = int(current_plan_len)
    if state.n_calls >= 2 and state.plan_len_std > 0:
        z = abs(plan_len - state.running_mean_plan_len) / state.plan_len_std
        verbosity_drift = float(min(3.0, z))
    else:
        verbosity_drift = 0.0

    # Now update state (Welford increments n_calls).
    state.update_plan_length(plan_len)
    state.record_drift(topic_drift)
    state.last_seen_ns = now
    if persist:
        save_session(state)

    return DriftSignals(
        topic_drift=topic_drift,
        verbosity_drift=verbosity_drift,
        n_calls=state.n_calls,
        is_anchor_call=is_anchor,
    )


# ─────────────────────────────────────────────────────────────────────
# Inspection helpers (used by `aegis session` CLI + tests)
# ─────────────────────────────────────────────────────────────────────


def list_sessions() -> list[dict[str, Any]]:
    """Return one summary dict per session file. Sorted by last_seen desc."""
    d = session_dir()
    if not d.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in d.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "session_id": data.get("session_id", p.stem),
            "n_calls": int(data.get("n_calls", 0)),
            "started_at_ns": int(data.get("started_at_ns", 0)),
            "last_seen_ns": int(data.get("last_seen_ns", 0)),
            "max_drift": (
                max(data.get("drift_history", [0.0]) or [0.0])
            ),
        })
    out.sort(key=lambda r: -int(r.get("last_seen_ns", 0)))
    return out


def clear_sessions(*, keep_recent: int = 0) -> int:
    """Delete all but the ``keep_recent`` most recently-touched sessions.

    Returns number of files removed. ``keep_recent=0`` deletes everything.
    """
    sessions = list_sessions()
    sessions.sort(key=lambda r: -int(r.get("last_seen_ns", 0)))
    keep_ids = {s["session_id"] for s in sessions[:keep_recent]}
    n_removed = 0
    d = session_dir()
    if not d.exists():
        return 0
    for p in d.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue
        try:
            sid = json.loads(p.read_text(encoding="utf-8")).get("session_id", "")
        except (OSError, json.JSONDecodeError):
            try:
                p.unlink()
                n_removed += 1
            except OSError:
                pass
            continue
        if sid in keep_ids:
            continue
        try:
            p.unlink()
            n_removed += 1
        except OSError:
            pass
    return n_removed


__all__ = [
    "DEFAULT_SESSION_DIR",
    "DriftSignals",
    "MAX_DRIFT_HISTORY",
    "SESSION_TTL_NS",
    "SessionState",
    "clear_sessions",
    "list_sessions",
    "load_session",
    "save_session",
    "session_dir",
    "update_and_score",
]
