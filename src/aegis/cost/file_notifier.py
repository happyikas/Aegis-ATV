"""File-based notifier — append every threshold crossing to a JSONL
file. The forensic complement to push notifiers (Slack, ntfy.sh):

* push notifiers tell you NOW
* this file lets you reconstruct WHEN/WHAT/HOW-MUCH later

Combined via :class:`aegis.cost.composite_notifier.CompositeNotifier`,
the typical production setup is::

    CompositeNotifier([
        NtfyNotifier(topic=...),                      # phone push
        FileNotifier(Path.home() / ".aegis" / "crossings.jsonl"),  # audit
        StderrNotifier(),                              # terminal
    ])

JSONL layout
------------

One line per crossing, fields::

    {
      "ts_ns":              int,       # crossing timestamp
      "label":              "warn" | "hard_stop" | custom,
      "threshold_dollars":  float,
      "fleet_dollars":      float,     # fleet total at crossing
      "aid_at_crossing":    str,       # which agent's call pushed over
      "call_idx":           int,       # global call index in fleet timeline
      "decision":           "continue" | "abort"
    }

Append-only. Tail with ``tail -F`` or replay with
``aegis cost summary --crossings-log``.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

from aegis.cost.multi_agent import Decision, FleetThreshold


class FileNotifier:
    """Append every crossing to a JSONL file. Returns the same
    decision policy as StderrNotifier (warn → continue, hard_stop →
    abort) by default; override with ``decision_policy``."""

    def __init__(
        self,
        path: Path,
        *,
        decision_policy: Callable[[FleetThreshold], Decision] | None = None,
        record_failures: bool = True,
    ) -> None:
        self.path = Path(path)
        self._decision_policy = decision_policy or _default_decision_policy
        self.record_failures = bool(record_failures)
        # Ensure parent dir exists at construction so the first write
        # doesn't blow up on a fresh ~/.aegis layout.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            if self.record_failures:
                sys.stderr.write(
                    f"[file-notifier] could not create parent {self.path.parent}: {e}\n"
                )
        self.n_writes_attempted: int = 0
        self.n_writes_succeeded: int = 0
        self.n_writes_failed: int = 0

    def on_threshold_crossing(
        self,
        *,
        threshold: FleetThreshold,
        fleet_dollars: float,
        aid: str,
        call_idx: int,
    ) -> Decision:
        decision = self._decision_policy(threshold)
        record = {
            "ts_ns": time.time_ns(),
            "label": threshold.label,
            "threshold_dollars": float(threshold.dollars),
            "fleet_dollars": float(fleet_dollars),
            "aid_at_crossing": str(aid),
            "call_idx": int(call_idx),
            "decision": decision,
        }
        self._append(record)
        return decision

    def _append(self, record: dict[str, object]) -> None:
        self.n_writes_attempted += 1
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
            self.n_writes_succeeded += 1
        except OSError as e:
            self.n_writes_failed += 1
            if self.record_failures:
                sys.stderr.write(f"[file-notifier] write failed: {e}\n")


def _default_decision_policy(threshold: FleetThreshold) -> Decision:
    return "abort" if threshold.label == "hard_stop" else "continue"
