"""Multi-agent (fleet) cost replay integration tests.

The headline scenario: **5 agents running concurrently**, each below
its own $1 ceiling, but the *fleet* sum crosses operator-set
thresholds at $5 (warn) and $10 (hard-stop). Verifies that the
notifier fires at the right call indexes with the right agent ids
and that the operator's continue / abort decision is honoured.

Reference for the production wiring:
:mod:`aegis.cost.multi_agent` (offline simulation harness; live
concurrent monitoring is a follow-up).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from aegis.cost.multi_agent import (
    AgentReplayInput,
    Decision,
    FleetThreshold,
    RecordingNotifier,
    StderrNotifier,
    multi_agent_replay,
)
from aegis.cost.replay import ReplayConfig

# ─────────────────────────────────────────────────────────────────────
# Synthetic agent fixture builders
# ─────────────────────────────────────────────────────────────────────


def _line(d: dict[str, Any]) -> str:
    return json.dumps(d) + "\n"


def synth_agent_transcript(
    path: Path,
    *,
    agent_idx: int,
    n_turns: int = 10,
    in_per_turn: int = 50_000,
    out_per_turn: int = 50_000,
    tool_name: str = "Bash",
) -> Path:
    """One agent's transcript — varying tool args per turn so step336
    loop detector stays silent. With Haiku 4.5 default FLOPs and the
    default ``in_per_turn=50k / out_per_turn=50k``, each turn adds
    roughly $2.55 → 10 turns ≈ $25.5 / agent / 5 agents = $127.5
    fleet (well above any threshold we'd want to test)."""
    parts: list[str] = [_line({"type": "user", "content": f"agent {agent_idx} task"})]
    for i in range(n_turns):
        rec = {
            "type": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {
                    "type": "tool_use",
                    "name": tool_name,
                    "input": {"command": f"agent_{agent_idx}_turn_{i}"},
                },
            ],
            "usage": {
                "input_tokens": in_per_turn,
                "output_tokens": out_per_turn,
            },
        }
        parts.append(_line(rec))
    path.write_text("".join(parts), encoding="utf-8")
    return path


@pytest.fixture
def five_agents(tmp_path: Path) -> list[AgentReplayInput]:
    """5 agents × 10 turns × 50k/50k tokens — each agent ~$2.55,
    fleet total ~$12.75. Enough headroom to test $5 warn + $10
    hard-stop thresholds."""
    inputs: list[AgentReplayInput] = []
    for i in range(1, 6):
        p = synth_agent_transcript(
            tmp_path / f"agent_{i}.jsonl", agent_idx=i, n_turns=10
        )
        inputs.append(AgentReplayInput(transcript_path=p, aid=f"agent-{i}"))
    return inputs


@pytest.fixture
def template_config(tmp_path: Path) -> ReplayConfig:
    """Generous per-agent budget so the fleet harness is the only
    gate that fires (per-agent step335 stays quiet)."""
    return ReplayConfig(
        transcript_path=tmp_path / "ignored.jsonl",
        budget_dollars=100.0,
        model_for_cost="claude-haiku-4-5",
    )


# ─────────────────────────────────────────────────────────────────────
# Core scenario — 5 agents → fleet threshold
# ─────────────────────────────────────────────────────────────────────


class TestFiveAgentScenario:
    """The user's headline ask: 5 agents accumulating, fleet exceeds
    threshold, real-time warning, ask whether to continue."""

    def test_fleet_cost_aggregates_across_agents(
        self,
        five_agents: list[AgentReplayInput],
        template_config: ReplayConfig,
    ) -> None:
        notifier = RecordingNotifier()
        thresholds = [FleetThreshold(dollars=5.0, label="warn")]
        s = multi_agent_replay(
            five_agents,
            thresholds=thresholds,
            config_template=template_config,
            notifier=notifier,
        )
        assert s.n_agents == 5
        # Sanity: fleet final ≈ sum of per-agent contributions.
        per_agent_sum = sum(s.per_agent_dollars.values())
        assert s.final_fleet_dollars == pytest.approx(
            per_agent_sum, rel=1e-9
        )
        # Each of the 5 agents has 10 calls → 50 fleet calls.
        assert s.n_total_calls == 50
        # Final fleet > $5 → warn must have fired at least once.
        assert s.final_fleet_dollars > 5.0
        assert len(s.crossings) == 1

    def test_warn_threshold_fires_with_correct_agent(
        self,
        five_agents: list[AgentReplayInput],
        template_config: ReplayConfig,
    ) -> None:
        notifier = RecordingNotifier()
        thresholds = [FleetThreshold(dollars=5.0, label="warn")]
        multi_agent_replay(
            five_agents,
            thresholds=thresholds,
            config_template=template_config,
            notifier=notifier,
        )
        # Notifier observed exactly one crossing.
        assert len(notifier.crossings) == 1
        threshold, fleet_dollars, aid, call_idx = notifier.crossings[0]
        assert threshold.dollars == 5.0
        assert fleet_dollars >= 5.0
        # The crossing must point at one of the 5 agents.
        assert aid in {f"agent-{i}" for i in range(1, 6)}
        # And at a global call index in [0, 50).
        assert 0 <= call_idx < 50

    def test_hard_stop_aborts_replay_at_threshold(
        self,
        five_agents: list[AgentReplayInput],
        template_config: ReplayConfig,
    ) -> None:
        notifier = RecordingNotifier()
        thresholds = [
            FleetThreshold(dollars=5.0, label="warn"),
            FleetThreshold(dollars=10.0, label="hard_stop"),
        ]
        s = multi_agent_replay(
            five_agents,
            thresholds=thresholds,
            config_template=template_config,
            notifier=notifier,
        )
        # Both thresholds crossed (warn at $5, hard-stop at $10).
        assert len(s.crossings) == 2
        warn = s.crossings[0]
        hard = s.crossings[1]
        assert warn.threshold.label == "warn"
        assert hard.threshold.label == "hard_stop"
        assert warn.operator_decision == "continue"
        assert hard.operator_decision == "abort"
        # Replay aborted at the hard-stop crossing (or before the next
        # call — either way < 50).
        assert s.aborted_at_call is not None
        assert s.aborted_at_call < 50

    def test_interactive_continue_lets_replay_finish(
        self,
        five_agents: list[AgentReplayInput],
        template_config: ReplayConfig,
    ) -> None:
        """Operator answers 'continue' even on hard-stop → replay
        runs to completion."""
        decisions: list[Decision] = ["continue", "continue"]
        notifier = RecordingNotifier(decisions=decisions)
        thresholds = [
            FleetThreshold(dollars=5.0, label="warn", interactive=True),
            FleetThreshold(dollars=10.0, label="hard_stop", interactive=True),
        ]
        s = multi_agent_replay(
            five_agents,
            thresholds=thresholds,
            config_template=template_config,
            notifier=notifier,
        )
        assert s.aborted_at_call is None
        assert s.n_total_calls == 50
        assert all(c.operator_decision == "continue" for c in s.crossings)

    def test_interactive_abort_stops_at_warn(
        self,
        five_agents: list[AgentReplayInput],
        template_config: ReplayConfig,
    ) -> None:
        """Operator can abort even on the WARN crossing if they want."""
        decisions: list[Decision] = ["abort"]
        notifier = RecordingNotifier(decisions=decisions)
        thresholds = [
            FleetThreshold(dollars=5.0, label="warn", interactive=True),
            FleetThreshold(dollars=10.0, label="hard_stop", interactive=True),
        ]
        s = multi_agent_replay(
            five_agents,
            thresholds=thresholds,
            config_template=template_config,
            notifier=notifier,
        )
        # Aborted at the warn crossing → only 1 crossing recorded.
        assert s.aborted_at_call is not None
        assert len(s.crossings) == 1
        assert s.crossings[0].threshold.label == "warn"
        assert s.crossings[0].operator_decision == "abort"


class TestPerAgentContribution:
    def test_per_agent_dollars_sum_equals_fleet_when_no_abort(
        self,
        five_agents: list[AgentReplayInput],
        template_config: ReplayConfig,
    ) -> None:
        """When the run completes, sum of per-agent finals == fleet final."""
        notifier = RecordingNotifier()
        s = multi_agent_replay(
            five_agents,
            thresholds=[FleetThreshold(dollars=1000.0, label="warn")],
            config_template=template_config,
            notifier=notifier,
        )
        assert s.aborted_at_call is None
        per_agent_sum = sum(s.per_agent_dollars.values())
        assert s.final_fleet_dollars == pytest.approx(
            per_agent_sum, rel=1e-9
        )
        # Every agent contributed.
        assert len(s.per_agent_dollars) == 5

    def test_per_agent_dollars_unaffected_by_abort(
        self,
        five_agents: list[AgentReplayInput],
        template_config: ReplayConfig,
    ) -> None:
        """The per-agent totals come from per-agent ``replay()``
        which always runs to completion (each agent's own step335 is
        independent of fleet abort)."""
        notifier = RecordingNotifier(decisions=["abort"])
        s = multi_agent_replay(
            five_agents,
            thresholds=[
                FleetThreshold(dollars=2.0, label="hard_stop", interactive=True),
            ],
            config_template=template_config,
            notifier=notifier,
        )
        assert s.aborted_at_call is not None
        # Even though fleet aborted early, every agent's transcript
        # was fully replayed → 5 entries with non-zero dollars.
        assert len(s.per_agent_dollars) == 5
        for aid, dollars in s.per_agent_dollars.items():
            assert dollars > 0.0, (
                f"agent {aid} should still have its own per-agent "
                f"cumulative even after fleet abort"
            )


class TestRoundRobinOrdering:
    """The merged timeline interleaves agents round-robin: A1, B1,
    C1, D1, E1, A2, B2, ... This ordering is deterministic so tests
    can assert the agent identity at any global call index."""

    def test_first_round_visits_each_agent_once(
        self,
        five_agents: list[AgentReplayInput],
        template_config: ReplayConfig,
    ) -> None:
        s = multi_agent_replay(
            five_agents,
            thresholds=[FleetThreshold(dollars=10_000.0)],
            config_template=template_config,
            notifier=RecordingNotifier(),
        )
        first_round_aids = [s.timeline[i].aid for i in range(5)]
        assert sorted(first_round_aids) == [
            f"agent-{i}" for i in range(1, 6)
        ]

    def test_global_idx_is_monotonic(
        self,
        five_agents: list[AgentReplayInput],
        template_config: ReplayConfig,
    ) -> None:
        s = multi_agent_replay(
            five_agents,
            thresholds=[FleetThreshold(dollars=10_000.0)],
            config_template=template_config,
            notifier=RecordingNotifier(),
        )
        idxs = [c.global_idx for c in s.timeline]
        assert idxs == list(range(len(idxs)))


# ─────────────────────────────────────────────────────────────────────
# StderrNotifier behaviour
# ─────────────────────────────────────────────────────────────────────


class TestStderrNotifier:
    def test_non_interactive_warn_continues_hard_stop_aborts(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        n = StderrNotifier(interactive=False)
        warn = FleetThreshold(dollars=5.0, label="warn")
        hard = FleetThreshold(dollars=10.0, label="hard_stop")
        d1 = n.on_threshold_crossing(
            threshold=warn, fleet_dollars=5.5, aid="a1", call_idx=3,
        )
        d2 = n.on_threshold_crossing(
            threshold=hard, fleet_dollars=10.5, aid="a2", call_idx=7,
        )
        assert d1 == "continue"
        assert d2 == "abort"
        err = capsys.readouterr().err
        assert "WARN" in err
        assert "HARD_STOP" in err
        assert "$=5.5000" in err
        assert "agent=a1" in err

    def test_interactive_yes_returns_continue(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
        n = StderrNotifier(interactive=True)
        d = n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.5, aid="a", call_idx=0,
        )
        assert d == "continue"

    def test_interactive_empty_stdin_returns_abort(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        n = StderrNotifier(interactive=True)
        d = n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.5, aid="a", call_idx=0,
        )
        assert d == "abort"

    def test_interactive_no_returns_abort(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
        n = StderrNotifier(interactive=True)
        d = n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.5, aid="a", call_idx=0,
        )
        assert d == "abort"


# ─────────────────────────────────────────────────────────────────────
# CLI E2E
# ─────────────────────────────────────────────────────────────────────


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # noqa: I001

    parser = aegis_cli.build_parser()
    ns = parser.parse_args(args)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_buf, err_buf
    try:
        rc = ns.fn(ns)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    return rc, out_buf.getvalue(), err_buf.getvalue()


class TestCLI:
    def test_multi_agent_table(self, tmp_path: Path) -> None:
        agents = []
        for i in range(1, 6):
            p = synth_agent_transcript(
                tmp_path / f"a{i}.jsonl", agent_idx=i, n_turns=10
            )
            agents.append(p)
        rc, out, _ = _run_cli([
            "cost", "multi-agent",
            "--transcripts", ",".join(str(p) for p in agents),
            "--threshold", "5.0",
            "--hard-stop", "10.0",
        ])
        # Hard-stop fires by design → rc 3.
        assert rc == 3
        assert "AegisData multi-agent cost replay" in out
        assert "agents:                 5" in out
        assert "ABORTED at fleet call" in out
        assert "Per-agent contribution:" in out
        # All 5 agent ids present in the table.
        for i in range(1, 6):
            assert f"agent-{i}" in out

    def test_multi_agent_json(self, tmp_path: Path) -> None:
        agents = []
        for i in range(1, 6):
            p = synth_agent_transcript(
                tmp_path / f"a{i}.jsonl", agent_idx=i, n_turns=10
            )
            agents.append(p)
        rc, out, _ = _run_cli([
            "cost", "multi-agent",
            "--transcripts", ",".join(str(p) for p in agents),
            "--threshold", "5.0",
            "--json",
        ])
        assert rc == 0
        payload = json.loads(out)
        assert payload["n_agents"] == 5
        assert payload["n_total_calls"] == 50
        assert payload["aborted_at_call"] is None
        # 5 agents in the per-agent payload.
        assert len(payload["per_agent_dollars"]) == 5

    def test_multi_agent_missing_transcript_exits_2(self, tmp_path: Path) -> None:
        rc, _, err = _run_cli([
            "cost", "multi-agent",
            "--transcripts", str(tmp_path / "nope.jsonl"),
        ])
        assert rc == 2
        assert "transcript not found" in err
