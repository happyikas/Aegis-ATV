"""Live fleet monitor tests — `aegis fleet-monitor` daemon.

The daemon is just :func:`process_new_records` polled in a loop, so
the bulk of the testing covers the pure record-processor with
synthesized audit JSONL inputs. A separate set of tests covers
state save/load roundtrip and the CLI lifecycle (start/status/stop)
end-to-end.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from aegis.cost.fleet_monitor import (
    FleetMonitorState,
    _load_state,
    _parse_cum_dollars,
    _save_state,
    process_new_records,
)
from aegis.cost.multi_agent import (
    FleetThreshold,
    RecordingNotifier,
)

# ─────────────────────────────────────────────────────────────────────
# Fixture: synthesize audit JSONL records in the canonical shape
# ─────────────────────────────────────────────────────────────────────


def _pretool_record(
    *, ts_ns: int, aid: str, tool: str = "Bash", cum_dollars: float = 0.0,
    decision: str = "ALLOW",
) -> dict[str, object]:
    return {
        "ts_ns": ts_ns,
        "tool": tool,
        "aid": aid,
        "decision": decision,
        "reason": "all firewall steps passed" if decision == "ALLOW" else (
            f"cumulative_dollars {cum_dollars:.4f} > budget 1.0000"
        ),
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


def _posttool_record(*, ts_ns: int, aid: str, tool: str) -> dict[str, object]:
    return {
        "ts_ns": ts_ns, "aid": aid, "tool": tool,
        "hook": "PostToolUse", "status": "success",
        "result_hash": "x" * 64, "exit_code": 0,
        "tool_input_keys": [], "mode": "local",
    }


def _write_lines(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


# ─────────────────────────────────────────────────────────────────────
# 1. _parse_cum_dollars — helper correctness
# ─────────────────────────────────────────────────────────────────────


class TestParseCumDollars:
    def test_step335_ok_trace_extracts_cum(self) -> None:
        rec = _pretool_record(ts_ns=1, aid="a", cum_dollars=0.42)
        assert _parse_cum_dollars(rec) == 0.42

    def test_overrun_reason_extracts_cum(self) -> None:
        rec = {
            "decision": "REQUIRE_APPROVAL",
            "reason": "cumulative_dollars 1.5000 > budget 1.0000",
        }
        assert _parse_cum_dollars(rec) == 1.5

    def test_no_cost_data_returns_none(self) -> None:
        rec = {"decision": "ALLOW", "explain": {"step_traces": {}}}
        assert _parse_cum_dollars(rec) is None


# ─────────────────────────────────────────────────────────────────────
# 2. process_new_records — single-pass, multi-pass, fleet aggregation
# ─────────────────────────────────────────────────────────────────────


class TestProcessNewRecords:
    def test_empty_audit_returns_zero(self, tmp_path: Path) -> None:
        state = FleetMonitorState()
        n = process_new_records(
            audit_path=tmp_path / "absent.jsonl",
            state=state,
            thresholds=[FleetThreshold(dollars=1.0)],
            notifier=RecordingNotifier(),
        )
        assert n == 0

    def test_fleet_aggregation_across_two_aids(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        # Two sessions interleaved.
        _write_lines(audit, [
            _pretool_record(ts_ns=1, aid="A", cum_dollars=0.50),
            _pretool_record(ts_ns=2, aid="B", cum_dollars=0.40),
            _pretool_record(ts_ns=3, aid="A", cum_dollars=1.20),
        ])
        state = FleetMonitorState()
        notifier = RecordingNotifier()
        n = process_new_records(
            audit_path=audit, state=state,
            thresholds=[FleetThreshold(dollars=1.0)],
            notifier=notifier,
        )
        assert n == 3
        # A: 0.50 → 1.20 (delta 0.70), B: 0 → 0.40 (delta 0.40)
        assert state.fleet_dollars == 0.50 + 0.40 + 0.70  # = 1.60
        # Threshold $1 crossed exactly once (at the third record).
        assert len(notifier.crossings) == 1
        threshold, fleet, aid, _ = notifier.crossings[0]
        assert threshold.dollars == 1.0
        assert aid == "A"   # the call that pushed it over
        assert fleet == state.fleet_dollars

    def test_post_tool_records_skipped(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_lines(audit, [
            _pretool_record(ts_ns=1, aid="A", cum_dollars=0.10),
            _posttool_record(ts_ns=2, aid="A", tool="Bash"),  # ignored
        ])
        state = FleetMonitorState()
        n = process_new_records(
            audit_path=audit, state=state,
            thresholds=[FleetThreshold(dollars=10.0)],
            notifier=RecordingNotifier(),
        )
        # Both lines bumped offset, but only the Pre record counts as
        # "decision-bearing".
        assert n == 1
        assert state.n_records_seen == 1

    def test_resume_from_offset_no_double_count(self, tmp_path: Path) -> None:
        """Second call after the first should process 0 new records."""
        audit = tmp_path / "audit.jsonl"
        _write_lines(audit, [
            _pretool_record(ts_ns=i, aid="A", cum_dollars=i * 0.10)
            for i in range(1, 6)
        ])
        state = FleetMonitorState()
        notifier = RecordingNotifier()
        n_first = process_new_records(
            audit_path=audit, state=state,
            thresholds=[FleetThreshold(dollars=100.0)],
            notifier=notifier,
        )
        n_second = process_new_records(
            audit_path=audit, state=state,
            thresholds=[FleetThreshold(dollars=100.0)],
            notifier=notifier,
        )
        assert n_first == 5
        assert n_second == 0

    def test_appended_records_picked_up_on_next_poll(
        self, tmp_path: Path
    ) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_lines(audit, [
            _pretool_record(ts_ns=1, aid="A", cum_dollars=0.10),
        ])
        state = FleetMonitorState()
        process_new_records(
            audit_path=audit, state=state,
            thresholds=[FleetThreshold(dollars=10.0)],
            notifier=RecordingNotifier(),
        )
        # Append more records — simulates Claude Code making more calls.
        _write_lines(audit, [
            _pretool_record(ts_ns=2, aid="A", cum_dollars=0.30),
            _pretool_record(ts_ns=3, aid="B", cum_dollars=0.50),
        ])
        n = process_new_records(
            audit_path=audit, state=state,
            thresholds=[FleetThreshold(dollars=10.0)],
            notifier=RecordingNotifier(),
        )
        assert n == 2
        assert state.fleet_dollars == 0.30 + 0.50  # = 0.80
        assert state.n_records_seen == 3


class TestThresholdFiring:
    def test_threshold_fires_exactly_once(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_lines(audit, [
            _pretool_record(ts_ns=i, aid="A", cum_dollars=i * 0.50)
            for i in range(1, 6)
        ])
        state = FleetMonitorState()
        notifier = RecordingNotifier()
        process_new_records(
            audit_path=audit, state=state,
            thresholds=[FleetThreshold(dollars=1.0)],
            notifier=notifier,
        )
        # Fleet 0.50 → 1.00 → 1.50 → 2.00 → 2.50; crosses $1 once.
        assert len(notifier.crossings) == 1

    def test_warn_then_hard_stop_in_order(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_lines(audit, [
            _pretool_record(ts_ns=i, aid="A", cum_dollars=i * 1.0)
            for i in range(1, 6)
        ])
        state = FleetMonitorState()
        notifier = RecordingNotifier()
        process_new_records(
            audit_path=audit, state=state,
            thresholds=[
                FleetThreshold(dollars=2.0, label="warn"),
                FleetThreshold(dollars=4.0, label="hard_stop"),
            ],
            notifier=notifier,
        )
        labels = [c[0].label for c in notifier.crossings]
        # warn fires when fleet ≥ 2 (at second call), hard_stop when ≥ 4.
        assert labels == ["warn", "hard_stop"]

    def test_hard_stop_writes_stop_flag(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_lines(audit, [
            _pretool_record(ts_ns=1, aid="A", cum_dollars=5.0),
        ])
        state = FleetMonitorState()
        # Default policy: hard_stop → abort decision.
        notifier = RecordingNotifier()
        stop_flag = tmp_path / "stop.json"
        process_new_records(
            audit_path=audit, state=state,
            thresholds=[FleetThreshold(dollars=2.0, label="hard_stop")],
            notifier=notifier,
            stop_flag=stop_flag,
        )
        assert stop_flag.is_file()
        flag = json.loads(stop_flag.read_text())
        assert flag["fleet_dollars"] == 5.0
        assert flag["aid_at_crossing"] == "A"


# ─────────────────────────────────────────────────────────────────────
# 3. State save / load roundtrip
# ─────────────────────────────────────────────────────────────────────


class TestStatePersistence:
    def test_save_then_load_matches(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        s = FleetMonitorState(
            last_offset=12345,
            per_aid_max_cum={"A": 0.50, "B": 1.20},
            fired_thresholds=[1.0],
            fleet_dollars=1.70,
            n_records_seen=10,
            last_seen_ts_ns=time.time_ns(),
        )
        _save_state(path, s)
        loaded = _load_state(path)
        assert loaded.last_offset == s.last_offset
        assert loaded.per_aid_max_cum == s.per_aid_max_cum
        assert loaded.fired_thresholds == s.fired_thresholds
        assert loaded.fleet_dollars == s.fleet_dollars

    def test_corrupt_state_falls_back_to_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.json"
        path.write_text("{ this is not valid json")
        loaded = _load_state(path)
        assert loaded.last_offset == 0
        assert loaded.fleet_dollars == 0.0

    def test_missing_state_returns_empty(self, tmp_path: Path) -> None:
        loaded = _load_state(tmp_path / "absent.json")
        assert loaded.last_offset == 0


# ─────────────────────────────────────────────────────────────────────
# 4. End-to-end: monitor catches up to a 5-agent burst
# ─────────────────────────────────────────────────────────────────────


class TestFiveAgentBurst:
    """User's headline scenario applied to the LIVE monitor: 5 agents
    each contributing toward a shared budget; warn at $5 / hard-stop
    at $10."""

    def test_five_agents_warn_then_abort(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        # Each of 5 agents makes 5 calls accumulating $1 each → $25 fleet.
        records = []
        for i in range(1, 6):       # 5 calls per agent
            for aid in ("a", "b", "c", "d", "e"):
                records.append(_pretool_record(
                    ts_ns=int(time.time_ns()) + len(records),
                    aid=aid,
                    cum_dollars=i * 1.0,
                ))
        _write_lines(audit, records)

        state = FleetMonitorState()
        notifier = RecordingNotifier()
        stop_flag = tmp_path / "stop.json"
        thresholds = [
            FleetThreshold(dollars=5.0, label="warn"),
            FleetThreshold(dollars=10.0, label="hard_stop"),
        ]
        process_new_records(
            audit_path=audit, state=state,
            thresholds=thresholds, notifier=notifier,
            stop_flag=stop_flag,
        )
        # Both thresholds fire exactly once.
        labels = [c[0].label for c in notifier.crossings]
        assert "warn" in labels
        assert "hard_stop" in labels
        assert len(labels) == 2
        # Stop flag written at hard_stop.
        assert stop_flag.is_file()
        # Fleet should be $25 total.
        assert state.fleet_dollars == 25.0
