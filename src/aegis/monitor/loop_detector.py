"""Per-session loop + redundant-call detector (v2.1.3, Day-1 #6).

Watches every ``observe(session_id, tool, args)`` and emits one of:

* ``"loop"``      — same (tool, args_hash) repeated >= ``loop_threshold``
                    times within the active session. Step336 turns this
                    into REQUIRE_APPROVAL the first time and BLOCK on
                    subsequent attempts.
* ``"redundant"`` — same read-only (Read / Grep / Glob / safe-listed
                    bash) call repeated within ``dedup_window``. Step336
                    leaves the call as ALLOW but flags the trace so the
                    risk report can show "N redundant calls deduped".
* ``None``        — fresh call.

Pure-Python, in-memory, lock-protected. No DB; the detector lifetime
matches the FastAPI app instance (sidecar) or the spawned hook
subprocess (local mode reaches it once per call so ``loop_threshold``
inevitably becomes 1 — the local-mode flag is intentionally informative
only there). The full per-session loop story plays out in sidecar mode
where the detector lives across PreToolUse calls.

Hash inputs are canonicalised JSON so equivalent dicts hash identically.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

# Tool names we consider read-only — repeating these is wasteful but
# never destructive. Aligned with policies/safe_actions.json.
_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "Read", "Grep", "Glob",
    "read_file", "search_files", "list_files",
    "FileRead", "OpenFile",
})


@dataclass
class LoopVerdict:
    """What :meth:`LoopDetector.observe` returns."""

    kind: str | None        # "loop" | "redundant" | None
    count: int              # how many times this exact call has been seen
    reason: str             # human-readable explanation
    args_hash: str          # the hash key (useful for the risk report)


def _canonical_hash(tool: str, args: dict[str, Any] | str) -> str:
    """SHA3-256 hex of (tool, canonical-JSON args)."""
    if isinstance(args, str):
        # Already-serialised tool_args_json — reparse to canonicalise.
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            parsed = args
    else:
        parsed = args
    canonical = json.dumps(
        {"tool": tool, "args": parsed},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha3_256(canonical.encode("utf-8")).hexdigest()


class LoopDetector:
    """Thread-safe per-session call counter + read-only dedup cache."""

    def __init__(
        self,
        *,
        loop_threshold: int = 3,
        dedup_window_secs: float = 300.0,
        retain_per_session: int = 1024,
    ) -> None:
        self.loop_threshold = loop_threshold
        self.dedup_window = dedup_window_secs
        self.retain_per_session = retain_per_session
        self._counts: dict[str, dict[str, int]] = defaultdict(dict)
        self._last_seen: dict[str, dict[str, float]] = defaultdict(dict)
        self._lock = threading.Lock()

    def observe(
        self,
        session_id: str,
        tool: str,
        args: dict[str, Any] | str,
    ) -> LoopVerdict:
        h = _canonical_hash(tool, args)
        now = time.time()
        with self._lock:
            self._counts[session_id][h] = self._counts[session_id].get(h, 0) + 1
            count = self._counts[session_id][h]
            previous_seen = self._last_seen[session_id].get(h)
            self._last_seen[session_id][h] = now
            self._gc_session_locked(session_id)

        if count >= self.loop_threshold:
            return LoopVerdict(
                kind="loop",
                count=count,
                reason=(
                    f"same {tool} call repeated {count} times this session "
                    f"(threshold={self.loop_threshold})"
                ),
                args_hash=h,
            )

        if (
            tool in _READ_ONLY_TOOLS
            and count >= 2
            and previous_seen is not None
            and (now - previous_seen) <= self.dedup_window
        ):
            return LoopVerdict(
                kind="redundant",
                count=count,
                reason=(
                    f"redundant read-only {tool} call (seen {count} times "
                    f"within {self.dedup_window:.0f}s)"
                ),
                args_hash=h,
            )

        return LoopVerdict(kind=None, count=count, reason="", args_hash=h)

    def stats(self, session_id: str) -> dict[str, Any]:
        """Aggregate stats for one session — used by ``aegis report``."""
        with self._lock:
            counts = dict(self._counts.get(session_id, {}))
        n_calls = sum(counts.values())
        n_unique = len(counts)
        n_loop = sum(1 for c in counts.values() if c >= self.loop_threshold)
        n_redundant = sum(c - 1 for c in counts.values() if c >= 2)
        return {
            "calls": n_calls,
            "unique_calls": n_unique,
            "looping_keys": n_loop,
            "redundant_calls": n_redundant,
        }

    def reset(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._counts.clear()
                self._last_seen.clear()
            else:
                self._counts.pop(session_id, None)
                self._last_seen.pop(session_id, None)

    def _gc_session_locked(self, session_id: str) -> None:
        """Drop oldest entries when a session exceeds ``retain_per_session``."""
        sess_seen = self._last_seen.get(session_id, {})
        if len(sess_seen) <= self.retain_per_session:
            return
        # Keep the most-recently-seen ``retain_per_session`` keys.
        ordered = sorted(sess_seen.items(), key=lambda kv: kv[1], reverse=True)
        keep = {k for k, _ in ordered[: self.retain_per_session]}
        for k in list(sess_seen.keys()):
            if k not in keep:
                sess_seen.pop(k, None)
                self._counts[session_id].pop(k, None)


# Module-level default detector — reused across all firewall step336
# invocations within a single FastAPI app instance.
_DEFAULT: LoopDetector | None = None
_DEFAULT_LOCK = threading.Lock()


def get_default_detector() -> LoopDetector:
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = LoopDetector()
    return _DEFAULT


def reset_default_detector() -> None:
    """Test helper — reset the module-level default detector."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None
