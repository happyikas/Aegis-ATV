"""Cost replay + summary integration tests.

These tests exercise :mod:`aegis.cost.replay` and :mod:`aegis.cost.summary`
plus the ``aegis cost`` CLI on realistic synthesized transcripts that
mirror common agent behaviour patterns:

* **marathon**       — slow accumulation across many small turns
* **large_read**     — a single tool_use that produces a massive output
                       token spike (think: ``Read`` of a megabyte file)
* **reasoning_loop** — heavy ``reasoning_tokens`` (extended thinking)
                       across many turns
* **cost_underreport** — token counts vs. HW-observed FLOPs disagree;
                          designed to trip M12 (Claim 27) under
                          ``--hw-provider sim --hw-attack cost_underreport``

The fixtures are generated programmatically (``_synth_*`` helpers) so
the test corpus stays in code rather than as opaque JSONL blobs.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from aegis.cost.replay import ReplayConfig, replay
from aegis.cost.summary import summarize

# ─────────────────────────────────────────────────────────────────────
# Fixture builders — realistic Claude Code transcript shapes
# ─────────────────────────────────────────────────────────────────────


def _line(d: dict[str, Any]) -> str:
    return json.dumps(d) + "\n"


def _user(text: str) -> str:
    return _line({"type": "user", "content": text})


def _assistant_with_tool_use(
    *,
    in_tokens: int = 0,
    out_tokens: int = 0,
    reasoning_tokens: int = 0,
    tool_name: str = "Bash",
    tool_input: dict[str, Any] | None = None,
) -> str:
    """One assistant turn carrying a tool_use block AND a usage block."""
    rec = {
        "type": "assistant",
        "content": [
            {"type": "text", "text": "I'll do that."},
            {
                "type": "tool_use",
                "name": tool_name,
                "input": tool_input or {"command": "ls"},
            },
        ],
        "usage": {
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "reasoning_tokens": reasoning_tokens,
        },
    }
    return _line(rec)


def synth_marathon(path: Path, *, n_turns: int = 50, tokens_per_turn: int = 200) -> Path:
    """``n_turns`` × ``tokens_per_turn`` tokens — long debugging session
    with small *varying* Bash calls.

    Defaults: 50 × 200 = 10 000 tokens cumulative. With Haiku 4.5
    FLOPs (1.4e10/in + 2.0e10/out) at $1.5e-15/FLOP that's about
    $0.255 cumulative — well under $1.0 default, an "honest agent"
    baseline that nonetheless accumulates noticeable cost.

    Tool args VARY per turn so step336's loop detector does NOT trip
    on identical-call repetition (which would mask the cost gate).
    """
    parts: list[str] = [_user("debug this for me")]
    in_per = tokens_per_turn // 2
    out_per = tokens_per_turn - in_per
    for i in range(n_turns):
        parts.append(
            _assistant_with_tool_use(
                in_tokens=in_per,
                out_tokens=out_per,
                tool_name="Bash",
                tool_input={"command": f"ls -la /tmp/work_{i}"},
            )
        )
    path.write_text("".join(parts), encoding="utf-8")
    return path


def synth_large_read(path: Path) -> Path:
    """50 huge turns: 200k input + 50k output → blows past $1.0 fast.

    Args vary per turn (different file paths) so step336's loop
    detector doesn't trip — we want the COST gate to fire, not the
    loop gate.
    """
    parts: list[str] = [_user("read these huge logs")]
    for i in range(50):
        parts.append(
            _assistant_with_tool_use(
                in_tokens=200_000,
                out_tokens=50_000,
                tool_name="Read",
                tool_input={"file_path": f"/var/log/huge_{i}.log"},
            )
        )
    path.write_text("".join(parts), encoding="utf-8")
    return path


def synth_reasoning_loop(path: Path) -> Path:
    """30 turns of heavy thinking — reasoning_tokens dominate.

    Each turn produces a unique todo list so step336 stays silent.
    """
    parts: list[str] = [_user("plan carefully")]
    for i in range(30):
        parts.append(
            _assistant_with_tool_use(
                in_tokens=1_000,
                out_tokens=500,
                reasoning_tokens=10_000,
                tool_name="TodoWrite",
                tool_input={"todos": [{"content": f"step-{i}", "status": "pending"}]},
            )
        )
    path.write_text("".join(parts), encoding="utf-8")
    return path


def synth_cost_underreport(path: Path) -> Path:
    """Small SW-reported tokens BUT a tool_use that the HW simulator
    will see as expensive — designed to trip M12 cost_underreport
    (Claim 27) when --hw-attack=cost_underreport is enabled."""
    parts: list[str] = [_user("execute this")]
    parts.append(
        _assistant_with_tool_use(
            in_tokens=50,
            out_tokens=20,
            tool_name="Bash",
            tool_input={"command": "compute_heavy --workload xxl"},
        )
    )
    path.write_text("".join(parts), encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────────────
# Replay tests — pure function, no CLI
# ─────────────────────────────────────────────────────────────────────


class TestReplayMarathon:
    def test_under_default_budget_all_allow(self, tmp_path: Path) -> None:
        transcript = synth_marathon(tmp_path / "marathon.jsonl")
        s = replay(ReplayConfig(transcript_path=transcript, budget_dollars=1.0))
        assert s.n_tool_calls == 50
        # Honest small calls — final cumulative under default $1 ceiling.
        assert s.final_cumulative_dollars < 1.0
        # Every call ALLOW because budget never breached. (Args vary
        # per turn so step336 loop detector stays silent.)
        assert s.n_allow == 50, (
            f"expected 50 ALLOW; got {s.n_allow}. "
            f"first non-ALLOW: turn={s.first_escalation_turn} "
            f"reason={s.calls[s.first_escalation_turn-1].reason if s.first_escalation_turn else ''!r}"
        )
        assert s.n_block == 0
        assert s.n_step335_escalations == 0

    def test_tight_budget_flips_decisions(self, tmp_path: Path) -> None:
        transcript = synth_marathon(tmp_path / "marathon.jsonl")
        # Below the natural cumulative — step335 will fire mid-session.
        s = replay(ReplayConfig(transcript_path=transcript, budget_dollars=0.001))
        assert s.n_tool_calls == 50
        assert s.n_approval > 0, "tight budget should trigger step335 escalations"
        assert s.first_escalation_turn is not None
        # Escalation reason carries 'cumulative_dollars' substring.
        escalated = [c for c in s.calls if c.decision == "REQUIRE_APPROVAL"]
        assert any("cumulative_dollars" in c.reason for c in escalated)


class TestReplayLargeRead:
    def test_immediate_escalation(self, tmp_path: Path) -> None:
        transcript = synth_large_read(tmp_path / "large_read.jsonl")
        s = replay(ReplayConfig(
            transcript_path=transcript, budget_dollars=1.0,
        ))
        assert s.n_tool_calls == 50
        # Real reasonable budget breached within first few turns.
        assert s.first_escalation_turn is not None
        assert s.first_escalation_turn <= 10
        # Final cumulative should be well past $1.0.
        assert s.final_cumulative_dollars > 1.0


class TestReplayReasoningLoop:
    def test_reasoning_tokens_count_toward_total(self, tmp_path: Path) -> None:
        transcript = synth_reasoning_loop(tmp_path / "reasoning.jsonl")
        s = replay(ReplayConfig(
            transcript_path=transcript, budget_dollars=10.0,
        ))
        assert s.n_tool_calls == 30
        # 30 × 10k reasoning tokens = 300k tokens reasoning total.
        # cumulative_tokens MUST include reasoning even though
        # cumulative_dollars uses the in/out tokens only.
        assert s.final_cumulative_tokens >= 300_000


class TestReplayHWAttackTriggersM12:
    """The cost_underreport attack injects HW-observed FLOPs that
    diverge from SW-reported tokens by >> 3× baseline → M12 should
    trigger the Claim 27 escalation."""

    def test_cost_underreport_attack_fires(self, tmp_path: Path) -> None:
        transcript = synth_cost_underreport(tmp_path / "underreport.jsonl")
        s = replay(ReplayConfig(
            transcript_path=transcript,
            budget_dollars=1.0,
            hw_provider="sim",
            hw_attack="cost_underreport",
        ))
        assert s.n_tool_calls == 1
        call = s.calls[0]
        assert call.cost_escalation_triggered, (
            "cost_underreport attack must trip M12 escalation"
        )
        assert call.cost_escalation_metric in (
            "token_to_flops", "memory_cost", "dollar_cost",
        )
        assert s.n_m12_escalations == 1
        # The verdict gets overridden from ALLOW to REQUIRE_APPROVAL
        # to match sidecar behaviour at evaluate.py:182.
        assert call.decision == "REQUIRE_APPROVAL"
        assert "cost-divergence escalation" in call.reason

    def test_no_attack_no_escalation(self, tmp_path: Path) -> None:
        """Honest sim run (no attack) should NOT trip M12."""
        transcript = synth_cost_underreport(tmp_path / "honest.jsonl")
        s = replay(ReplayConfig(
            transcript_path=transcript,
            budget_dollars=1.0,
            hw_provider="sim",
            hw_attack="",   # honest
        ))
        assert s.n_tool_calls == 1
        assert s.n_m12_escalations == 0


class TestReplayMissingTranscript:
    def test_missing_file_returns_empty_summary(self, tmp_path: Path) -> None:
        s = replay(ReplayConfig(transcript_path=tmp_path / "nope.jsonl"))
        assert s.n_tool_calls == 0
        assert s.calls == []


class TestReplayRealClaudeCodeShape:
    """Claude Code's actual transcript JSONL nests usage + content under
    a ``message`` key (and tool_use blocks live inside ``message.content[]``,
    not as top-level events). Regression test: the replay harness must
    handle both the fixture-flat shape and Claude's real nested shape."""

    def _real_shape_transcript(self, path: Path) -> Path:
        """Synthesize one nested-shape assistant turn carrying a
        tool_use block inside message.content, with usage stored under
        message.usage including cache_* fields."""
        rec_user = {"type": "user", "content": "do it"}
        rec_assistant = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [
                    {"type": "thinking", "thinking": "let me think"},
                    {"type": "text", "text": "I'll run a command."},
                    {
                        "type": "tool_use",
                        "id": "toolu_xyz",
                        "name": "Bash",
                        "input": {"command": "ls -la"},
                    },
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 5_000,
                    "cache_creation_input_tokens": 200,
                },
            },
        }
        path.write_text(
            _line(rec_user) + _line(rec_assistant), encoding="utf-8"
        )
        return path

    def test_nested_message_shape_extracts_tool_use_and_tokens(
        self, tmp_path: Path
    ) -> None:
        transcript = self._real_shape_transcript(tmp_path / "real.jsonl")
        s = replay(ReplayConfig(
            transcript_path=transcript, budget_dollars=10.0,
        ))
        # The single tool_use inside message.content[] must be picked up.
        assert s.n_tool_calls == 1
        assert s.calls[0].tool_name == "Bash"
        # Cumulative tokens MUST include cache_* (5_000 + 200 + 100 + 50)
        # so cost_estimate is honest about what the model actually ran.
        assert s.calls[0].cumulative_tokens == pytest.approx(5_350.0)
        # cumulative_dollars > 0 proves the tokens flowed all the way
        # into step335's budget gate (not the 0.00 plugin-mode bug).
        assert s.calls[0].cumulative_dollars > 0.0


# ─────────────────────────────────────────────────────────────────────
# Summary tests — pure function over an audit JSONL
# ─────────────────────────────────────────────────────────────────────


def _write_audit(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _pretool_record(
    *, ts_ns: int, aid: str, tool: str, decision: str = "ALLOW",
    cum_dollars: float = 0.0, reason: str = "all firewall steps passed",
) -> dict[str, Any]:
    return {
        "ts_ns": ts_ns,
        "tool": tool,
        "aid": aid,
        "decision": decision,
        "reason": reason,
        "trace_id": f"t-{ts_ns}",
        "latency_ms": 5.0,
        "mode": "local",
        "explain": {
            "atv_dim": 2080,
            "atv_sha3": "a" * 64,
            "step_traces": {
                "aegis.firewall.step335_cost.run":
                    f"step335: ok (cum={cum_dollars:.4f}, "
                    f"forecast=0.0000, ceiling=1.0000, burn=0.00)",
            },
        },
    }


class TestSummary:
    def test_aggregates_decisions_and_max_cum(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _pretool_record(ts_ns=1, aid="A", tool="Bash", cum_dollars=0.10),
            _pretool_record(ts_ns=2, aid="A", tool="Bash", cum_dollars=0.20),
            _pretool_record(
                ts_ns=3, aid="A", tool="Bash",
                decision="REQUIRE_APPROVAL",
                cum_dollars=1.50,
                reason="cumulative_dollars 1.5000 > budget 1.0000",
            ),
            _pretool_record(ts_ns=4, aid="B", tool="Read", cum_dollars=0.05),
        ])
        s = summarize(audit)
        assert s.n_records_total == 4
        assert s.n_pretool == 4
        assert s.n_allow == 3
        assert s.n_approval == 1
        assert s.max_cumulative_dollars == pytest.approx(1.50, abs=1e-9)
        assert s.n_step335_escalations == 1

    def test_per_tool_and_per_session(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _pretool_record(ts_ns=1, aid="A", tool="Bash", cum_dollars=0.50),
            _pretool_record(ts_ns=2, aid="A", tool="Read", cum_dollars=0.55),
            _pretool_record(ts_ns=3, aid="B", tool="Bash", cum_dollars=0.10),
        ])
        s = summarize(audit)
        # per-tool sorted by max_cumulative_dollars desc
        tool_names = [t.tool for t in s.per_tool]
        assert "Bash" in tool_names and "Read" in tool_names
        # session A has higher max
        assert s.per_session[0].aid == "A"
        assert s.per_session[0].max_cumulative_dollars == pytest.approx(0.55)

    def test_spike_detection(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _pretool_record(ts_ns=1, aid="A", tool="Bash", cum_dollars=0.01),
            _pretool_record(ts_ns=2, aid="A", tool="Read", cum_dollars=0.03),
            _pretool_record(ts_ns=3, aid="A", tool="Read", cum_dollars=0.50),  # spike
        ])
        s = summarize(audit, spike_threshold=0.10)
        assert len(s.spike_events) == 1
        ev = s.spike_events[0]
        assert ev["aid"] == "A"
        assert ev["from_dollars"] == pytest.approx(0.03)
        assert ev["to_dollars"] == pytest.approx(0.50)

    def test_m12_escalation_counted(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _pretool_record(
                ts_ns=1, aid="A", tool="Bash",
                decision="REQUIRE_APPROVAL",
                cum_dollars=0.05,
                reason=(
                    "cost-divergence escalation: token_to_flops = 0.890 > "
                    "threshold 0.300 (3.0× baseline 0.100)."
                ),
            ),
        ])
        s = summarize(audit)
        assert s.n_m12_escalations == 1

    def test_missing_audit_returns_empty(self, tmp_path: Path) -> None:
        s = summarize(tmp_path / "absent.jsonl")
        assert s.n_records_total == 0
        assert s.per_tool == []


# ─────────────────────────────────────────────────────────────────────
# CLI E2E — drive `aegis cost {summary,replay}` via argparse
# ─────────────────────────────────────────────────────────────────────


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    """Invoke the CLI in-process via build_parser + dispatch.

    Captures stdout and stderr without spawning a subprocess so we
    keep the test fast and deterministic.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # noqa: I001  (intentional path manipulation)

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
    def test_cost_summary_table(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _pretool_record(ts_ns=1, aid="A", tool="Bash", cum_dollars=0.30),
            _pretool_record(ts_ns=2, aid="A", tool="Bash", cum_dollars=0.45),
        ])
        rc, out, _ = _run_cli(["cost", "summary", "--audit", str(audit)])
        assert rc == 0
        assert "AegisData cost summary" in out
        assert "0.4500" in out  # max_cumulative_dollars

    def test_cost_summary_json(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _pretool_record(ts_ns=1, aid="A", tool="Bash", cum_dollars=0.10),
        ])
        rc, out, _ = _run_cli(
            ["cost", "summary", "--audit", str(audit), "--json"]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["n_pretool"] == 1
        assert payload["max_cumulative_dollars"] == pytest.approx(0.10)

    def test_cost_summary_empty_audit_graceful(self, tmp_path: Path) -> None:
        rc, out, _ = _run_cli(
            ["cost", "summary", "--audit", str(tmp_path / "absent.jsonl")]
        )
        assert rc == 0
        assert "no records" in out

    def test_cost_replay_marathon(self, tmp_path: Path) -> None:
        transcript = synth_marathon(tmp_path / "m.jsonl")
        rc, out, _ = _run_cli([
            "cost", "replay", str(transcript),
            "--budget", "1.0",
        ])
        assert rc == 0
        assert "AegisData cost replay" in out
        assert "tool calls:" in out
        # 50 ALLOW lines means the table has rendered every turn.
        assert out.count("ALLOW") >= 50

    def test_cost_replay_tight_budget_table_shows_escalations(
        self, tmp_path: Path
    ) -> None:
        transcript = synth_marathon(tmp_path / "m.jsonl")
        rc, out, _ = _run_cli([
            "cost", "replay", str(transcript),
            "--budget", "0.001",
        ])
        assert rc == 0
        assert "REQUIRE_APPROVAL" in out
        assert "step335 hits:" in out

    def test_cost_replay_json(self, tmp_path: Path) -> None:
        transcript = synth_marathon(
            tmp_path / "m.jsonl", n_turns=5, tokens_per_turn=1_000
        )
        rc, out, _ = _run_cli([
            "cost", "replay", str(transcript),
            "--budget", "1.0",
            "--json",
        ])
        assert rc == 0
        payload = json.loads(out)
        assert payload["n_tool_calls"] == 5
        assert payload["config"]["budget_dollars"] == 1.0
        assert isinstance(payload["calls"], list)
        assert len(payload["calls"]) == 5

    def test_cost_replay_missing_transcript(self, tmp_path: Path) -> None:
        rc, _, _ = _run_cli([
            "cost", "replay", str(tmp_path / "nope.jsonl"),
        ])
        assert rc == 2
