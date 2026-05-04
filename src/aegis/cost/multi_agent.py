"""Multi-agent (fleet) cost replay + threshold notifier.

Single-agent ``aegis cost replay`` (#35) measures one transcript at a
time. In production, a coding-AI deployment is more often **N agents
running concurrently** — each with its own session id, all sharing
one operator. The honest question is *fleet*-level: when does the
combined cumulative dollar across N agents cross the operator's
budget, and how do we surface that crossing to a human while
optionally letting them decide whether to continue?

This module provides the offline harness for that question. It runs
each agent's transcript through the existing :func:`replay` (so per-
agent firewall decisions remain authentic), then merges the per-agent
:class:`ReplayCall` lists into a single fleet timeline, tracks
running fleet dollars, and fires a callback when a threshold is
crossed. The callback decides ``continue`` or ``abort`` — letting
tests plug in a deterministic policy and the CLI plug in an
interactive stdin prompt.

This is **simulation**, not a live monitor. A live concurrent-agent
plugin is a larger follow-up; today the harness validates the
threshold + notifier logic and gives operators a what-if tool.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from aegis.cost.replay import ReplayCall, ReplayConfig, replay

Decision = Literal["continue", "abort"]


@dataclass(frozen=True)
class AgentReplayInput:
    """One agent in the fleet — its own transcript and stable id."""

    transcript_path: Path
    aid: str


@dataclass(frozen=True)
class FleetThreshold:
    """A dollar level that, when fleet cumulative crosses it, fires
    the notifier."""

    dollars: float
    label: str = "warn"            # "warn" | "hard_stop" | custom
    interactive: bool = False      # ask the operator continue/abort?


@dataclass
class FleetCrossing:
    """One threshold-crossing event — recorded for forensic review."""

    threshold: FleetThreshold
    crossed_at_call: int            # global index in the merged timeline
    aid_at_crossing: str
    fleet_dollars_before: float
    fleet_dollars_after: float
    operator_decision: Decision     # what the notifier returned


@dataclass
class FleetCall:
    """A single tool call in the merged fleet timeline."""

    global_idx: int
    aid: str
    call: ReplayCall                # the underlying single-agent call
    fleet_dollars_after: float      # running fleet total post-this-call


@dataclass
class MultiAgentReplaySummary:
    n_agents: int
    n_total_calls: int = 0
    final_fleet_dollars: float = 0.0
    per_agent_dollars: dict[str, float] = field(default_factory=dict)
    crossings: list[FleetCrossing] = field(default_factory=list)
    aborted_at_call: int | None = None      # set if any notifier returned abort
    timeline: list[FleetCall] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Notifier protocol + reference implementations
# ─────────────────────────────────────────────────────────────────────


class Notifier(Protocol):
    """A notifier receives every threshold crossing and returns a
    decision. Stays out of the way otherwise."""

    def on_threshold_crossing(
        self,
        *,
        threshold: FleetThreshold,
        fleet_dollars: float,
        aid: str,
        call_idx: int,
    ) -> Decision:
        ...


class StderrNotifier:
    """Default real-world notifier — prints a banner to stderr.

    Behaviour:

    * Non-interactive (default): warn-only thresholds → return
      ``continue``; hard-stop thresholds → return ``abort``. This
      matches the operator-mental-model of ``--threshold`` (warn) and
      ``--hard-stop`` (abort).
    * Interactive: read y/N from stdin. Empty / EOF / non-yes →
      ``abort``. Use this for ``aegis cost multi-agent --interactive``
      so the operator decides per crossing.
    """

    def __init__(self, *, interactive: bool = False) -> None:
        self.interactive = interactive

    def on_threshold_crossing(
        self,
        *,
        threshold: FleetThreshold,
        fleet_dollars: float,
        aid: str,
        call_idx: int,
    ) -> Decision:
        sys.stderr.write(
            f"\n[fleet-cost] {threshold.label.upper()}  "
            f"fleet $={fleet_dollars:.4f}  crossed threshold "
            f"$={threshold.dollars:.4f}  (agent={aid}, call#{call_idx})\n"
        )
        sys.stderr.flush()

        if self.interactive:
            sys.stderr.write("  continue running the fleet? [y/N] ")
            sys.stderr.flush()
            try:
                line = sys.stdin.readline()
            except (EOFError, KeyboardInterrupt):
                line = ""
            answer = (line or "").strip().lower()
            return "continue" if answer in ("y", "yes") else "abort"

        # Non-interactive default: warn → keep going, hard_stop → abort.
        return "abort" if threshold.label == "hard_stop" else "continue"


class RecordingNotifier:
    """Test notifier — pre-programmed decisions, records every crossing.

    Useful for asserting "the warn fired exactly once at $5.10 with
    the right agent and the right global call index" without coupling
    tests to stderr capture.
    """

    def __init__(self, decisions: list[Decision] | None = None) -> None:
        self._decisions: list[Decision] = list(decisions or [])
        self.crossings: list[
            tuple[FleetThreshold, float, str, int]
        ] = []

    def on_threshold_crossing(
        self,
        *,
        threshold: FleetThreshold,
        fleet_dollars: float,
        aid: str,
        call_idx: int,
    ) -> Decision:
        self.crossings.append((threshold, fleet_dollars, aid, call_idx))
        if self._decisions:
            return self._decisions.pop(0)
        # Default: warn → continue, hard_stop → abort.
        return "abort" if threshold.label == "hard_stop" else "continue"


# ─────────────────────────────────────────────────────────────────────
# Core
# ─────────────────────────────────────────────────────────────────────


def _interleave_round_robin(
    per_agent: dict[str, list[ReplayCall]],
) -> list[tuple[str, ReplayCall]]:
    """Merge N per-agent call lists into one fleet timeline.

    Round-robin by call index: agent A turn 1, agent B turn 1, …,
    agent A turn 2, … This is a deterministic stand-in for "concurrent
    agents doing one call each round" — production would key on real
    timestamps, but our offline harness preserves causal ordering
    within each agent and gives every agent equal slice of fleet
    progress.
    """
    aids = list(per_agent.keys())
    if not aids:
        return []
    max_len = max(len(per_agent[a]) for a in aids)
    out: list[tuple[str, ReplayCall]] = []
    for round_idx in range(max_len):
        for aid in aids:
            calls = per_agent[aid]
            if round_idx < len(calls):
                out.append((aid, calls[round_idx]))
    return out


def _crossings_in_step(
    *,
    before: float,
    after: float,
    fired: set[float],
    thresholds: tuple[FleetThreshold, ...],
) -> list[FleetThreshold]:
    """Return the thresholds that ``before → after`` newly crosses,
    in ascending dollar order, excluding any already-fired."""
    out = []
    for t in sorted(thresholds, key=lambda x: x.dollars):
        if t.dollars in fired:
            continue
        if before < t.dollars <= after:
            out.append(t)
    return out


def multi_agent_replay(
    agents: list[AgentReplayInput],
    *,
    thresholds: list[FleetThreshold],
    config_template: ReplayConfig,
    notifier: Notifier | None = None,
) -> MultiAgentReplaySummary:
    """Run each agent's transcript through the firewall, merge into a
    fleet timeline, and fire the notifier on every threshold crossing.

    ``config_template`` provides the budget / model / HW knobs shared
    by all agents. Each agent gets its own copy with ``transcript_path``
    swapped in. Per-agent ``budget_dollars`` stays the same (so step335
    on each agent independently fires its own budget gate); the
    fleet-level threshold is what *this* function tracks.

    Returns a complete summary including:

    * per-agent final cumulative dollars (independent of fleet)
    * the merged fleet timeline (one row per global call)
    * the threshold crossings + the operator decision at each
    * ``aborted_at_call`` if a hard-stop notifier returned abort

    The notifier is called synchronously, in the order crossings happen.
    Multiple thresholds can cross in a single call (e.g. a $14 spike
    blowing past both $5 and $10) — they fire in ascending dollar
    order, and ``abort`` from any one of them stops the timeline
    immediately.
    """
    notifier = notifier or StderrNotifier(interactive=False)

    # 1) Run each agent through single-agent replay. Per-agent step335
    #    decisions remain independent (each agent has its own
    #    cumulative_dollars vs config_template.budget_dollars).
    per_agent_calls: dict[str, list[ReplayCall]] = {}
    per_agent_dollars: dict[str, float] = {}
    for agent in agents:
        cfg = ReplayConfig(
            transcript_path=agent.transcript_path,
            budget_dollars=config_template.budget_dollars,
            model_for_cost=config_template.model_for_cost,
            hw_provider=config_template.hw_provider,
            hw_attack=config_template.hw_attack,
            multiplier=config_template.multiplier,
        )
        s = replay(cfg)
        per_agent_calls[agent.aid] = list(s.calls)
        per_agent_dollars[agent.aid] = s.final_cumulative_dollars

    # 2) Interleave round-robin into one fleet timeline.
    merged = _interleave_round_robin(per_agent_calls)

    summary = MultiAgentReplaySummary(
        n_agents=len(agents),
        per_agent_dollars=per_agent_dollars,
    )

    # 3) Walk the timeline, accumulating fleet $, firing crossings.
    fleet_dollars = 0.0
    fired_thresholds: set[float] = set()
    threshold_tuple = tuple(thresholds)

    for global_idx, (aid, call) in enumerate(merged):
        # The per-agent cumulative is monotone, so the *delta* at this
        # step equals (this call's cum) − (prev call's cum for this aid).
        # Find the prior call for the same agent in the merged stream:
        prev_cum_for_aid = 0.0
        for j in range(global_idx - 1, -1, -1):
            if merged[j][0] == aid:
                prev_cum_for_aid = merged[j][1].cumulative_dollars
                break
        delta = max(0.0, call.cumulative_dollars - prev_cum_for_aid)

        before = fleet_dollars
        fleet_dollars += delta
        after = fleet_dollars

        summary.timeline.append(
            FleetCall(
                global_idx=global_idx,
                aid=aid,
                call=call,
                fleet_dollars_after=after,
            )
        )
        summary.n_total_calls += 1

        # Threshold crossings.
        crossed = _crossings_in_step(
            before=before, after=after,
            fired=fired_thresholds, thresholds=threshold_tuple,
        )
        abort_now = False
        for t in crossed:
            decision = notifier.on_threshold_crossing(
                threshold=t,
                fleet_dollars=after,
                aid=aid,
                call_idx=global_idx,
            )
            fired_thresholds.add(t.dollars)
            summary.crossings.append(
                FleetCrossing(
                    threshold=t,
                    crossed_at_call=global_idx,
                    aid_at_crossing=aid,
                    fleet_dollars_before=before,
                    fleet_dollars_after=after,
                    operator_decision=decision,
                )
            )
            if decision == "abort":
                abort_now = True
                break

        if abort_now:
            summary.aborted_at_call = global_idx
            break

    summary.final_fleet_dollars = fleet_dollars
    return summary
