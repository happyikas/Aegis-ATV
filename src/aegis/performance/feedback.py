"""Closed-loop performance feedback store (v3.2).

The runtime reports actual per-turn perf metrics to ``/tool-outcome``;
those values feed the next ATV's cost band so s-10 (cache_hit_rate)
and s-11 (context_utilization_ratio) reflect *measured* reality
instead of the host's possibly-stale self-report.

Design
------
* **Per (tenant_id, aid) EWMA** — exponential moving average so old
  observations decay. ``alpha = 0.30`` (recent observation weighted
  ~30 %, history ~70 %). Tunable per-deployment.
* **In-memory only** — feedback is local rolling state, not audit
  truth. Each Aegis process keeps its own. Restart resets to zero
  (matches the "advisor confidence collapses with no signal" path).
* **Thread-safe** — single ``threading.Lock`` covers reads + writes.
* **Optional override** — host can pre-fill the cost band on
  ``ATVInput`` and the feedback store will *not* overwrite it.

Patent linkage
--------------
Closed-loop perf attestation: the runtime's measured metrics become
inputs to the next ATV. With T3 hardware (M19+), the runtime can
emit the same metrics over the cost-attestation key (Claim 34) so
the feedback is itself signed and auditable.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

_DEFAULT_ALPHA = 0.30


@dataclass
class _PerfState:
    """Per-key rolling state. Floats default to 0 (no signal)."""

    cache_hit_rate: float = 0.0
    context_utilization_ratio: float = 0.0
    tokens_per_second: float = 0.0
    runtime_latency_ms: float = 0.0
    memory_peak_bytes: float = 0.0
    sample_count: int = 0
    last_updated_ns: int = 0


@dataclass
class PerfFeedback:
    """Snapshot of the perf signals for a single (tenant_id, aid)."""

    cache_hit_rate: float
    context_utilization_ratio: float
    tokens_per_second: float
    runtime_latency_ms: float
    memory_peak_bytes: float
    sample_count: int
    last_updated_ns: int

    def is_empty(self) -> bool:
        return self.sample_count == 0


class PerfFeedbackStore:
    """Thread-safe per-key EWMA store of runtime perf metrics."""

    def __init__(self, *, alpha: float = _DEFAULT_ALPHA) -> None:
        if not (0.0 < alpha <= 1.0):
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self._alpha = alpha
        self._states: dict[tuple[str, str], _PerfState] = {}
        self._lock = threading.Lock()

    def update(
        self,
        *,
        tenant_id: str,
        aid: str,
        cache_hit_rate: float | None = None,
        context_utilization_ratio: float | None = None,
        tokens_per_second: float | None = None,
        runtime_latency_ms: float | None = None,
        memory_peak_bytes: float | None = None,
    ) -> PerfFeedback:
        """Fold a new observation into the EWMA. Missing fields are skipped."""
        key = (tenant_id, aid)
        a = self._alpha
        with self._lock:
            st = self._states.get(key)
            if st is None:
                st = _PerfState()
                self._states[key] = st
            if cache_hit_rate is not None:
                st.cache_hit_rate = a * cache_hit_rate + (1 - a) * st.cache_hit_rate
            if context_utilization_ratio is not None:
                st.context_utilization_ratio = (
                    a * context_utilization_ratio
                    + (1 - a) * st.context_utilization_ratio
                )
            if tokens_per_second is not None:
                st.tokens_per_second = a * tokens_per_second + (1 - a) * st.tokens_per_second
            if runtime_latency_ms is not None:
                st.runtime_latency_ms = a * runtime_latency_ms + (1 - a) * st.runtime_latency_ms
            if memory_peak_bytes is not None:
                st.memory_peak_bytes = a * memory_peak_bytes + (1 - a) * st.memory_peak_bytes
            st.sample_count += 1
            st.last_updated_ns = time.time_ns()
            return _to_snapshot(st)

    def get(self, *, tenant_id: str, aid: str) -> PerfFeedback:
        key = (tenant_id, aid)
        with self._lock:
            st = self._states.get(key)
            if st is None:
                return PerfFeedback(
                    cache_hit_rate=0.0,
                    context_utilization_ratio=0.0,
                    tokens_per_second=0.0,
                    runtime_latency_ms=0.0,
                    memory_peak_bytes=0.0,
                    sample_count=0,
                    last_updated_ns=0,
                )
            return _to_snapshot(st)

    def reset(self) -> None:
        """Drop all rolling state (test helper, ops trigger)."""
        with self._lock:
            self._states.clear()

    @property
    def alpha(self) -> float:
        return self._alpha


def _to_snapshot(st: _PerfState) -> PerfFeedback:
    return PerfFeedback(
        cache_hit_rate=st.cache_hit_rate,
        context_utilization_ratio=st.context_utilization_ratio,
        tokens_per_second=st.tokens_per_second,
        runtime_latency_ms=st.runtime_latency_ms,
        memory_peak_bytes=st.memory_peak_bytes,
        sample_count=st.sample_count,
        last_updated_ns=st.last_updated_ns,
    )


# Process-wide singleton — `aegis.api.tool_outcome` writes to this,
# `aegis.api.evaluate` / `aegis.api.advisory` read from it.
_DEFAULT_STORE: PerfFeedbackStore = PerfFeedbackStore()


def get_default_store() -> PerfFeedbackStore:
    return _DEFAULT_STORE


def reset_default_store() -> None:
    """Test helper — drop all rolling state in the singleton."""
    _DEFAULT_STORE.reset()


__all__ = [
    "PerfFeedback",
    "PerfFeedbackStore",
    "get_default_store",
    "reset_default_store",
]
