"""Composite notifier — fan a single threshold crossing out to N
sub-notifiers (Slack + stderr + DB recorder + …) and aggregate their
decisions conservatively.

Use case
--------

Production deployments usually want both:

* a **broadcast** channel so the whole team sees the crossing
  (Slack / PagerDuty / email)
* a **prompt** channel so the operator can decide continue / abort
  (stderr + stdin via ``StderrNotifier(interactive=True)``)
* a **forensic record** (``RecordingNotifier``)

This module composes them without coupling. The aggregator is
*conservative*: if **any** sub-notifier returns ``abort``, the
composite returns ``abort``. This is the safer default — Slack
never overrides a human's "no, stop" decision.
"""

from __future__ import annotations

from collections.abc import Sequence

from aegis.cost.multi_agent import Decision, FleetThreshold, Notifier


class CompositeNotifier:
    """Fan-out wrapper. Calls every sub-notifier in declared order;
    returns ``abort`` if any returned ``abort``, otherwise
    ``continue``."""

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        if not notifiers:
            raise ValueError(
                "CompositeNotifier needs at least one sub-notifier"
            )
        self._notifiers: tuple[Notifier, ...] = tuple(notifiers)
        # Public counters for audits.
        self.n_calls: int = 0
        self.n_aborted: int = 0

    def on_threshold_crossing(
        self,
        *,
        threshold: FleetThreshold,
        fleet_dollars: float,
        aid: str,
        call_idx: int,
    ) -> Decision:
        self.n_calls += 1
        # Run every sub-notifier — even after one says "abort" — so a
        # Slack alert still goes out for forensic record even if the
        # human said stop. Aggregate decision after the fan-out.
        decisions: list[Decision] = []
        for n in self._notifiers:
            d = n.on_threshold_crossing(
                threshold=threshold,
                fleet_dollars=fleet_dollars,
                aid=aid,
                call_idx=call_idx,
            )
            decisions.append(d)
        result: Decision = "abort" if any(d == "abort" for d in decisions) else "continue"
        if result == "abort":
            self.n_aborted += 1
        return result
