"""Session-prior calibration — risk-label scoped autonomy thresholds.

Autonomy idea #5: a one-size-fits-all `min_trust` is wrong. When
the operator is *exploring* a new repo, false positives on
REQUIRE_APPROVAL are pure friction — they want loose bypass. When
they're shipping a *production deploy*, false negatives are
catastrophic — they want every approval click.

v0.5.25 lets the operator tag the current work session with a
risk label at start:

* ``exploring``    — loose: ``min_trust = 0.70``. Casual coding,
                     POC work, anything where being wrong about
                     a bypass costs at most a re-do.
* ``refactor``     — default: ``min_trust = 0.85``. Standard
                     development. Same threshold as if no label
                     were set.
* ``prod-deploy``  — strict: ``min_trust = 0.95``. Release work,
                     migrations, anything touching production. The
                     autonomy bypass virtually never fires in this
                     mode; the operator sees every approval.

The label persists in ``~/.aegis/autonomy/session_prior.json``
with a default 8-hour expiry. After expiry the label is treated
as absent and the bypass falls back to the standard threshold.

This is a UX feature, not a safety feature: the never-trust
filter, reversibility gate, drift gate, and andon tripwire all
fire independently. Session-prior just modulates the trust score
threshold for normal patterns.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# ──────────────────────────────────────────────────────────────────
# Risk labels
# ──────────────────────────────────────────────────────────────────

RISK_LABELS: Final[tuple[str, ...]] = ("exploring", "refactor", "prod-deploy")
"""Valid labels. Order matters for the operator-facing CLI help."""

_DEFAULT_LABEL: Final[str] = "refactor"
"""Used when the env / state is missing — equivalent to "no
session-prior", which means the standard MIN_TRUST_FOR_BYPASS
applies."""

_LABEL_TO_MIN_TRUST: Final[dict[str, float]] = {
    "exploring": 0.70,
    "refactor": 0.85,
    "prod-deploy": 0.95,
}
"""Per-label min_trust threshold. Tuned to:
   - exploring: 5pp BELOW default (more bypasses fire)
   - refactor:  AT default
   - prod-deploy: 10pp ABOVE default (essentially nothing fires)"""

DEFAULT_TTL_HOURS: Final[int] = 8
"""Session-prior auto-expires after 8 hours unless renewed. This
matches a typical workday — the operator doesn't have to
remember to call `aegis autonomy session end` at sign-off."""


# ──────────────────────────────────────────────────────────────────
# Path resolution
# ──────────────────────────────────────────────────────────────────


def session_prior_path() -> Path:
    """On-disk location of the session-prior state. Honours
    ``AEGIS_AUTONOMY_SESSION_PRIOR`` for tests / multi-tenant."""
    raw = os.environ.get("AEGIS_AUTONOMY_SESSION_PRIOR", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".aegis" / "autonomy" / "session_prior.json"


# ──────────────────────────────────────────────────────────────────
# State dataclass
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SessionPrior:
    """The tagged work-session label. Frozen so callers can pass
    it freely without worrying about mutation."""

    label: str = _DEFAULT_LABEL
    started_at_ns: int = 0
    expires_at_ns: int = 0
    note: str = ""

    def is_expired(self, *, now_ns: int | None = None) -> bool:
        if self.expires_at_ns <= 0:
            return False  # no expiry set (legacy / explicit infinite)
        ts = now_ns if now_ns is not None else time.time_ns()
        return ts >= self.expires_at_ns

    def is_default(self) -> bool:
        """True when this prior is the implicit default (no
        operator action). Lets callers distinguish 'operator
        explicitly set refactor' from 'no session_prior file'."""
        return self.label == _DEFAULT_LABEL and self.started_at_ns == 0


# ──────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────


def load_session_prior(
    path: Path | None = None,
) -> SessionPrior:
    """Read the on-disk session-prior. Returns the default
    (``refactor``, no expiry, no metadata) when the file is
    missing or unparseable. Expired entries also return the
    default."""
    target = path if path is not None else session_prior_path()
    if not target.exists():
        return SessionPrior()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SessionPrior()
    if not isinstance(payload, dict):
        return SessionPrior()
    label = str(payload.get("label", _DEFAULT_LABEL))
    if label not in RISK_LABELS:
        label = _DEFAULT_LABEL
    state = SessionPrior(
        label=label,
        started_at_ns=int(payload.get("started_at_ns", 0) or 0),
        expires_at_ns=int(payload.get("expires_at_ns", 0) or 0),
        note=str(payload.get("note", "") or "")[:200],
    )
    if state.is_expired():
        return SessionPrior()
    return state


def _save_session_prior(state: SessionPrior, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({
                "label": state.label,
                "started_at_ns": state.started_at_ns,
                "expires_at_ns": state.expires_at_ns,
                "note": state.note,
            }),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


def start_session(
    label: str,
    *,
    note: str = "",
    ttl_hours: float = DEFAULT_TTL_HOURS,
    path: Path | None = None,
    now_ns: int | None = None,
) -> SessionPrior:
    """Tag the current work session with a risk label. Returns
    the persisted :class:`SessionPrior`. Raises ``ValueError`` if
    the label is unknown — the CLI validates first, so this is a
    programmer-error path."""
    if label not in RISK_LABELS:
        raise ValueError(
            f"label must be one of {RISK_LABELS}; got {label!r}",
        )
    target = path if path is not None else session_prior_path()
    ts = now_ns if now_ns is not None else time.time_ns()
    expiry = (
        ts + int(ttl_hours * 3600 * 1_000_000_000) if ttl_hours > 0 else 0
    )
    state = SessionPrior(
        label=label,
        started_at_ns=ts,
        expires_at_ns=expiry,
        note=note[:200],
    )
    _save_session_prior(state, target)
    return state


def end_session(path: Path | None = None) -> None:
    """Clear the session-prior. The bypass reverts to the standard
    threshold immediately."""
    target = path if path is not None else session_prior_path()
    try:
        if target.exists():
            target.unlink()
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────
# Threshold lookup
# ──────────────────────────────────────────────────────────────────


def session_min_trust(
    fallback: float,
    *,
    path: Path | None = None,
) -> tuple[float, SessionPrior]:
    """Return ``(min_trust, prior)`` for the current session.

    If an explicit session-prior is active, returns its label-
    scoped threshold. Otherwise returns ``fallback`` (typically
    the caller's standard ``MIN_TRUST_FOR_BYPASS``).

    Always returns the loaded :class:`SessionPrior` too so the
    runtime can stamp the label into ``step_traces`` for audit."""
    prior = load_session_prior(path)
    if prior.is_default():
        return fallback, prior
    return _LABEL_TO_MIN_TRUST.get(prior.label, fallback), prior


__all__ = [
    "DEFAULT_TTL_HOURS",
    "RISK_LABELS",
    "SessionPrior",
    "end_session",
    "load_session_prior",
    "session_min_trust",
    "session_prior_path",
    "start_session",
]
