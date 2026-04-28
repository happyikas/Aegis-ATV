"""Per-session runtime monitors (v2.1.3, partial D7 port).

This package owns the in-memory observers that watch agent behavior
across multiple tool calls within a single session. The first
inhabitant is :mod:`aegis.monitor.loop_detector` — a lock-protected
counter of (tool, args_hash) tuples per session that surfaces:

* **loop**     — same call repeated >= ``loop_threshold`` times.
* **redundant** — same read-only call repeated <2 times within the
  dedup window (cheap cache opportunity).

Future inhabitants (planned for v2.1.x / v2.2):

* ``malfunction.py`` (D7) — error_rate / atv_loop / schema_drift
  health classifier (signal in {ok, warn, critical}).
"""

from __future__ import annotations

from aegis.monitor.loop_detector import (
    LoopDetector,
    LoopVerdict,
    get_default_detector,
    reset_default_detector,
)

__all__ = [
    "LoopDetector",
    "LoopVerdict",
    "get_default_detector",
    "reset_default_detector",
]
