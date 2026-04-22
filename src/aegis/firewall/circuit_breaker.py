"""Per-AID circuit breaker (patent ¶[0063L]-[0063M]).

Tracks AID-tag-comparator violations + voluntary quarantines. When an
AID exceeds its configured violation budget, the controller masks new
DMA / CXL / tool calls for that AID, freezes uncommitted transaction
records, and permits only forensic-read access until released by a
signed administrative recovery policy.

T2 implementation is software emulation: in-process counters + JSON
config in policies/aid_region.json. T3 has the same logic in the
hardware tag comparator on the CSD.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum


class AidStatus(StrEnum):
    NORMAL = "normal"
    QUARANTINED = "quarantined"


@dataclass
class AidState:
    aid: str
    status: AidStatus = AidStatus.NORMAL
    violations: int = 0
    last_violation_ns: int = 0
    quarantined_at_ns: int = 0
    quarantine_reason: str = ""
    history: list[dict[str, object]] = field(default_factory=list)


class CircuitBreaker:
    """Thread-safe per-AID counter + status tracker."""

    def __init__(self) -> None:
        self._states: dict[str, AidState] = {}
        self._lock = threading.Lock()

    # ---------- counter ----------
    def record_violation(self, aid: str, *, max_allowed: int, reason: str) -> AidState:
        """Increment the violation counter; auto-quarantine if over budget."""
        now = time.time_ns()
        with self._lock:
            st = self._states.setdefault(aid, AidState(aid=aid))
            st.violations += 1
            st.last_violation_ns = now
            st.history.append({"kind": "violation", "ts_ns": now, "reason": reason})
            if st.violations >= max_allowed and st.status != AidStatus.QUARANTINED:
                st.status = AidStatus.QUARANTINED
                st.quarantined_at_ns = now
                st.quarantine_reason = (
                    f"violations {st.violations} ≥ max {max_allowed}: {reason}"
                )
                st.history.append({
                    "kind": "quarantine", "ts_ns": now,
                    "reason": st.quarantine_reason,
                })
            return st

    # ---------- status ----------
    def is_quarantined(self, aid: str) -> bool:
        with self._lock:
            st = self._states.get(aid)
            return bool(st and st.status == AidStatus.QUARANTINED)

    def _snapshot_locked(self, st: AidState) -> AidState:
        """Snapshot helper — assumes the caller already holds ``self._lock``."""
        return AidState(
            aid=st.aid, status=st.status, violations=st.violations,
            last_violation_ns=st.last_violation_ns,
            quarantined_at_ns=st.quarantined_at_ns,
            quarantine_reason=st.quarantine_reason,
            history=list(st.history),
        )

    def get(self, aid: str) -> AidState | None:
        with self._lock:
            st = self._states.get(aid)
            return self._snapshot_locked(st) if st is not None else None

    def list_quarantined(self) -> list[AidState]:
        with self._lock:
            return [
                self._snapshot_locked(s)
                for s in self._states.values()
                if s.status == AidStatus.QUARANTINED
            ]

    # ---------- admin release ----------
    def release(self, aid: str, *, reason: str) -> AidState | None:
        """Manually release an AID from quarantine. ¶[0063M] requires
        a signed administrative recovery policy / human approval; the
        signature check is layered on top by the API endpoint."""
        now = time.time_ns()
        with self._lock:
            st = self._states.get(aid)
            if st is None:
                return None
            if st.status != AidStatus.QUARANTINED:
                return self._snapshot_locked(st)
            st.status = AidStatus.NORMAL
            st.violations = 0
            st.history.append({
                "kind": "release", "ts_ns": now, "reason": reason,
            })
            return self._snapshot_locked(st)
