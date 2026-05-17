"""Andon tripwire — consecutive-bypass cap (v0.5.24).

Autonomy idea #4: even if every bypass decision is individually
correct, a long run of silent bypasses can erode the operator's
awareness of *what* is being approved on their behalf. Toyota's
andon-cord pattern says: periodically force a halt so a human
re-engages.

v0.5.24 maintains a consecutive-bypass counter at
``~/.aegis/autonomy/andon_state.json``. After ``N`` (default 20)
successive auto-approvals, the next bypass is refused — the human
sees the prompt, makes a conscious decision, and the counter
resets to zero.

### Why a separate file rather than in-process state

* The hook is a fresh process per Claude Code tool call. In-
  process state would reset to zero every call, defeating the
  purpose. The file persists across invocations.
* The trust table is the operator's *batch* artefact. The andon
  counter is *transient* runtime state — different cadence, so
  it lives in its own file.

### Hot-path safety

* Read returns ``0`` on any error (missing file / parse failure
  / OS read error). Better to under-fire than to block on I/O.
* Write swallows OSError. A failed write means the counter
  doesn't increment — same effect as if no bypass happened.
* Never raises.

### Operator override

Set ``AEGIS_AUTONOMY_ANDON_THRESHOLD=0`` to disable the tripwire
entirely. Values in [1, 1000] are honoured; out-of-range falls
back to the default."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# ──────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────

DEFAULT_ANDON_THRESHOLD: Final[int] = 20
"""Consecutive bypasses after which the next one is forced to the
human. 20 is conservative — at ε=0.05 the next forced exploration
would arrive in ~20 calls anyway, so this is a backup safety net
in case the operator disabled ε-greedy."""


def andon_state_path() -> Path:
    """Return the on-disk path for the andon counter. Honours
    ``AEGIS_AUTONOMY_ANDON_STATE`` for tests / multi-tenant."""
    raw = os.environ.get("AEGIS_AUTONOMY_ANDON_STATE", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".aegis" / "autonomy" / "andon_state.json"


def andon_threshold_from_env(
    default: int = DEFAULT_ANDON_THRESHOLD,
) -> int:
    """Read the threshold from the env. Clamped to [0, 1000].
    Zero disables the tripwire entirely."""
    raw = os.environ.get("AEGIS_AUTONOMY_ANDON_THRESHOLD", "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    if v < 0 or v > 1000:
        return default
    return v


# ──────────────────────────────────────────────────────────────────
# State persistence
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AndonState:
    """The on-disk counter shape. Frozen so callers compose new
    states rather than mutating."""

    consecutive_bypasses: int = 0
    last_bypass_ns: int = 0
    last_andon_ns: int = 0


def load_state(path: Path | None = None) -> AndonState:
    """Read the counter. Returns the default (zero) state on any
    error — see module docstring."""
    target = path if path is not None else andon_state_path()
    if not target.exists():
        return AndonState()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AndonState()
    if not isinstance(payload, dict):
        return AndonState()
    return AndonState(
        consecutive_bypasses=int(
            payload.get("consecutive_bypasses", 0) or 0,
        ),
        last_bypass_ns=int(payload.get("last_bypass_ns", 0) or 0),
        last_andon_ns=int(payload.get("last_andon_ns", 0) or 0),
    )


def _save_state(state: AndonState, path: Path) -> None:
    """Atomic write. Swallows OSError — see module docstring."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({
                "consecutive_bypasses": state.consecutive_bypasses,
                "last_bypass_ns": state.last_bypass_ns,
                "last_andon_ns": state.last_andon_ns,
            }),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────
# Decision API
# ──────────────────────────────────────────────────────────────────


def should_fire_andon(
    *,
    threshold: int | None = None,
    path: Path | None = None,
) -> tuple[bool, AndonState]:
    """Check whether the next bypass would trip the andon. Returns
    ``(should_fire, current_state)``.

    Caller still has to:
      * If ``should_fire`` is True → refuse the bypass and call
        :func:`record_andon` to reset the counter.
      * If ``should_fire`` is False AND the bypass actually
        engages → call :func:`record_bypass` to increment.
      * If the bypass is declined for other reasons (low trust,
        drift, etc.) — do NOT call anything; the counter stays.

    This deliberate three-way contract keeps the counter coherent
    with what actually happened on the hot path."""
    actual_threshold = (
        threshold if threshold is not None else andon_threshold_from_env()
    )
    if actual_threshold == 0:
        return False, AndonState()
    state = load_state(path)
    fire = state.consecutive_bypasses >= actual_threshold
    return fire, state


def record_bypass(
    state: AndonState,
    *,
    path: Path | None = None,
    now_ns: int | None = None,
) -> AndonState:
    """Increment the counter after a successful bypass. Persists.
    Returns the new state."""
    target = path if path is not None else andon_state_path()
    ts = now_ns if now_ns is not None else time.time_ns()
    new_state = AndonState(
        consecutive_bypasses=state.consecutive_bypasses + 1,
        last_bypass_ns=ts,
        last_andon_ns=state.last_andon_ns,
    )
    _save_state(new_state, target)
    return new_state


def record_andon(
    state: AndonState,
    *,
    path: Path | None = None,
    now_ns: int | None = None,
) -> AndonState:
    """Reset the counter after the tripwire fires. Persists."""
    target = path if path is not None else andon_state_path()
    ts = now_ns if now_ns is not None else time.time_ns()
    new_state = AndonState(
        consecutive_bypasses=0,
        last_bypass_ns=state.last_bypass_ns,
        last_andon_ns=ts,
    )
    _save_state(new_state, target)
    return new_state


def reset_counter(path: Path | None = None) -> AndonState:
    """Force-reset the counter. Used by CLI + tests."""
    target = path if path is not None else andon_state_path()
    new_state = AndonState()
    _save_state(new_state, target)
    return new_state


__all__ = [
    "DEFAULT_ANDON_THRESHOLD",
    "AndonState",
    "andon_state_path",
    "andon_threshold_from_env",
    "load_state",
    "record_andon",
    "record_bypass",
    "reset_counter",
    "should_fire_andon",
]
