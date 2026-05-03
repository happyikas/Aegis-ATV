"""Unit tests for the per-scenario ATV report writer."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from aegis.atv.builder import build_atv
from aegis.atv.report_writer import (
    _m13_top_contributors,
    _summarise_subfields,
    build_report,
    to_markdown,
    write_report,
)
from aegis.schema import (
    ALL_SUBFIELDS,
    ATV_DIM,
    ATVHeader,
    ATVInput,
    CostEfficiencyMetrics,
    Verdict,
)


def _input() -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t-test", span_id="s-test",
            tenant_id="demo-tenant", aid="agent-x",
            timestamp_ns=time.time_ns(),
        ),
        agent_state_text="test agent state",
        plan_text="test plan",
        tool_name="Bash",
        tool_args_json='{"command":"ls"}',
        safety_flags={"prompt_injection": 0.05},
        memory_fingerprint="sha3:test",
        cost_estimate=CostEfficiencyMetrics(input_token_count=10, output_token_count=5),
    )


def _verdict(decision: str = "ALLOW", **traces: str) -> Verdict:
    return Verdict(
        decision=decision,  # type: ignore[arg-type]
        reason=traces.get("__reason__", "test"),
        atv_id="test-atv",
        step_traces={k: v for k, v in traces.items() if not k.startswith("__")},
    )


class TestSummariseSubfields:
    def test_returns_30_rows(self) -> None:
        atv = build_atv(_input())
        rows, _ = _summarise_subfields(atv)
        assert len(rows) == len(ALL_SUBFIELDS) == 30

    def test_each_row_has_required_keys(self) -> None:
        atv = build_atv(_input())
        rows, _ = _summarise_subfields(atv)
        for row in rows:
            assert set(row.keys()) >= {"subfield", "slice_start", "slice_stop", "non_zero", "max_abs"}

    def test_nonzero_count_matches_actual(self) -> None:
        atv = build_atv(_input())
        rows, nz = _summarise_subfields(atv)
        manual = sum(1 for r in rows if r["non_zero"])
        assert nz == manual

    def test_zero_atv_yields_zero_nonzero(self) -> None:
        atv = np.zeros(ATV_DIM, dtype=np.float32)
        rows, nz = _summarise_subfields(atv)
        assert nz == 0
        assert all(r["non_zero"] is False for r in rows)


class TestM13TopContributors:
    def test_no_attribution_returns_empty(self) -> None:
        v = _verdict("ALLOW", run="step340: ok")
        assert _m13_top_contributors(v) == []

    def test_parses_attribution_segment(self) -> None:
        trace = (
            "step340: hybrid REQUIRE_APPROVAL "
            "attribution=tool_arg_inspection:0.30,action_blast_radius:0.21"
        )
        v = _verdict("REQUIRE_APPROVAL", run=trace)
        out = _m13_top_contributors(v, k=5)
        assert len(out) == 2
        assert out[0]["subfield"] == "tool_arg_inspection"
        assert out[0]["weight"] == pytest.approx(0.30)
        # sorted desc
        assert out[0]["weight"] >= out[1]["weight"]

    def test_truncates_at_k(self) -> None:
        trace = "attribution=" + ",".join(f"sf_{i}:{i*0.1:.2f}" for i in range(10))
        v = _verdict("ALLOW", run=trace)
        assert len(_m13_top_contributors(v, k=3)) == 3


class TestBuildReport:
    def test_full_report_structure(self) -> None:
        inp = _input()
        atv = build_atv(inp)
        v = _verdict("BLOCK", step340="step340: BLOCK keyword=drop")
        r = build_report(
            scenario_id=1, title="Test scenario", real_incident="test",
            inp=inp, atv=atv, verdict=v,
            expected_decision={"BLOCK", "REQUIRE_APPROVAL"}, pass_fail="PASS",
            latency_ms=42.0,
        )
        assert r.scenario_id == 1
        assert r.decision == "BLOCK"
        assert r.pass_fail == "PASS"
        assert r.atv_dim == ATV_DIM
        assert len(r.subfield_coverage) == 30
        assert r.n_steps_blocking == 1  # step340 has BLOCK keyword
        assert r.latency_ms == 42.0
        assert r.expected_decision == ["BLOCK", "REQUIRE_APPROVAL"]  # sorted

    def test_extras_passed_through(self) -> None:
        inp = _input()
        atv = build_atv(inp)
        r = build_report(
            scenario_id=2, title="X", real_incident="Y",
            inp=inp, atv=atv, verdict=_verdict("ALLOW"),
            expected_decision={"ALLOW"}, pass_fail="PASS",
            extras={"adapter": "enhanced", "k": 1},
        )
        assert r.extras == {"adapter": "enhanced", "k": 1}

    def test_atv_sha3_is_hex(self) -> None:
        inp = _input()
        atv = build_atv(inp)
        r = build_report(
            scenario_id=1, title="x", real_incident="y",
            inp=inp, atv=atv, verdict=_verdict(),
            expected_decision={"ALLOW"}, pass_fail="PASS",
        )
        assert len(r.atv_sha3) == 64  # SHA3-256 hex
        int(r.atv_sha3, 16)  # parses


class TestToMarkdown:
    def test_renders_basic_sections(self) -> None:
        inp = _input()
        atv = build_atv(inp)
        r = build_report(
            scenario_id=1, title="My Scenario", real_incident="some incident",
            inp=inp, atv=atv, verdict=_verdict("BLOCK"),
            expected_decision={"BLOCK"}, pass_fail="PASS",
        )
        md = to_markdown(r)
        assert "Scenario 1 — My Scenario" in md
        assert "## 1. Tool invocation" in md
        assert "## 2. Verdict" in md
        assert "## 3. ATV-2080 coverage" in md
        assert "## 5. Firewall step traces" in md
        assert "✅" in md  # PASS badge

    def test_fail_badge_shown_for_fail(self) -> None:
        inp = _input()
        atv = build_atv(inp)
        r = build_report(
            scenario_id=1, title="X", real_incident="Y",
            inp=inp, atv=atv, verdict=_verdict("ALLOW"),
            expected_decision={"BLOCK"}, pass_fail="FAIL",
        )
        assert "❌" in to_markdown(r)


class TestWriteReport:
    def test_writes_md_and_json(self, tmp_path: Path) -> None:
        inp = _input()
        atv = build_atv(inp)
        r = build_report(
            scenario_id=1, title="X", real_incident="Y",
            inp=inp, atv=atv, verdict=_verdict("ALLOW"),
            expected_decision={"ALLOW"}, pass_fail="PASS",
        )
        paths = write_report(r, tmp_path)
        assert "md" in paths and "json" in paths
        assert paths["md"].exists()
        assert paths["json"].exists()
        # json must round-trip
        data = json.loads(paths["json"].read_text())
        assert data["scenario_id"] == 1
        assert data["schema_version"] == "atv-report-v1"

    def test_creates_missing_directory(self, tmp_path: Path) -> None:
        inp = _input()
        atv = build_atv(inp)
        r = build_report(
            scenario_id=1, title="X", real_incident="Y",
            inp=inp, atv=atv, verdict=_verdict(),
            expected_decision={"ALLOW"}, pass_fail="PASS",
        )
        nested = tmp_path / "deeply" / "nested" / "out"
        write_report(r, nested)
        assert nested.exists()
        assert any(nested.glob("scenario_1_*.md"))

    def test_format_filter_md_only(self, tmp_path: Path) -> None:
        inp = _input()
        atv = build_atv(inp)
        r = build_report(
            scenario_id=1, title="X", real_incident="Y",
            inp=inp, atv=atv, verdict=_verdict(),
            expected_decision={"ALLOW"}, pass_fail="PASS",
        )
        paths = write_report(r, tmp_path, formats=("md",))
        assert "md" in paths and "json" not in paths
        assert not any(tmp_path.glob("*.json"))
