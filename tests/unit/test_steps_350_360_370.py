"""Tests for Firewall steps 350 (approval dispatch) / 360 (audit) /
370 (exec annotation) — patent ¶[0061]-[0063], M9 split."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.sqlite_store import AuditDB
from aegis.firewall import step350_approval, step360_audit, step370_exec
from aegis.schema import (
    ATV_DIM,
    ATVHeader,
    ATVInput,
    CostEfficiencyMetrics,
    Verdict,
)
from aegis.sign.ed25519 import load_or_create_key


def _inp(**over: object) -> ATVInput:
    base: dict[str, object] = dict(
        header=ATVHeader(
            trace_id="t", span_id="s", tenant_id="demo-tenant",
            aid="a", timestamp_ns=time.time_ns(),
        ),
        plan_text="plan",
        tool_name="read_file",
        tool_args_json='{"path":"./data/x.txt"}',
    )
    base.update(over)
    return ATVInput(**base)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────
# Step 350 — approval dispatch
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _silent_channel() -> None:
    step350_approval.set_channel("silent")
    step350_approval.drain_emitted()  # reset buffer before each test


class TestStep350Dispatch:
    def test_allow_noop(self) -> None:
        v = Verdict(decision="ALLOW", reason="", atv_id="x", step_traces={})
        req = step350_approval.dispatch(v, _inp())
        assert req is None
        assert step350_approval.drain_emitted() == []

    def test_block_noop(self) -> None:
        v = Verdict(decision="BLOCK", reason="nope", atv_id="x", step_traces={})
        req = step350_approval.dispatch(v, _inp())
        assert req is None
        assert step350_approval.drain_emitted() == []

    def test_approval_dispatches_and_buffers(self) -> None:
        v = Verdict(
            decision="REQUIRE_APPROVAL",
            reason="blast 10 >= 7",
            atv_id="atv-123",
            step_traces={"step330": "approval required (blast=10)"},
        )
        inp = _inp(tool_name="transfer_funds")
        req = step350_approval.dispatch(v, inp)
        assert req is not None
        assert req.atv_id == "atv-123"
        assert req.tool_name == "transfer_funds"
        assert req.reason == "blast 10 >= 7"
        assert req.aid == "a"
        emitted = step350_approval.drain_emitted()
        assert len(emitted) == 1
        assert emitted[0] is req


# ─────────────────────────────────────────────────────────────────────
# Step 360 — sign + audit append
# ─────────────────────────────────────────────────────────────────────

class TestStep360Audit:
    def test_happy_path_signs_and_appends(self, tmp_path: Path) -> None:
        key = load_or_create_key(tmp_path / "k.pem")
        db = AuditDB(":memory:")
        log = JsonlStore(tmp_path / "audit.jsonl")
        atv = np.zeros(ATV_DIM, dtype=np.float32)
        v = Verdict(decision="ALLOW", reason="ok", atv_id="atv-1", step_traces={})
        rec = step360_audit.sign_and_append(
            atv=atv, verdict=v, inp=_inp(), key=key, db=db, log=log
        )
        assert rec["signature"]
        assert rec["this_hash"]
        assert rec["atv_id"] == "atv-1"
        assert rec["cost_attestation_hint"] is False
        chain = db.get_chain("a")
        assert len(chain) == 1
        assert chain[0]["signature"] == rec["signature"]

    def test_cost_hint_flips_when_step335_in_traces(self, tmp_path: Path) -> None:
        key = load_or_create_key(tmp_path / "k.pem")
        db = AuditDB(":memory:")
        log = JsonlStore(tmp_path / "audit.jsonl")
        atv = np.zeros(ATV_DIM, dtype=np.float32)
        v = Verdict(
            decision="REQUIRE_APPROVAL",
            reason="forecast 5.0 > 1.0",
            atv_id="atv-2",
            step_traces={
                "aegis.firewall.step335_cost.run": "step335: forecast over ceiling",
            },
        )
        rec = step360_audit.sign_and_append(
            atv=atv, verdict=v, inp=_inp(), key=key, db=db, log=log
        )
        assert rec["cost_attestation_hint"] is True

    def test_approaching_ceiling_also_flags_cost_hint(self, tmp_path: Path) -> None:
        key = load_or_create_key(tmp_path / "k.pem")
        db = AuditDB(":memory:")
        log = JsonlStore(tmp_path / "audit.jsonl")
        atv = np.zeros(ATV_DIM, dtype=np.float32)
        v = Verdict(
            decision="ALLOW",
            reason="ok",
            atv_id="atv-3",
            step_traces={
                "aegis.firewall.step335_cost.run": "step335: approaching ceiling (forecast 0.85)",
            },
        )
        rec = step360_audit.sign_and_append(
            atv=atv, verdict=v, inp=_inp(cost_estimate=CostEfficiencyMetrics()),
            key=key, db=db, log=log,
        )
        assert rec["cost_attestation_hint"] is True


# ─────────────────────────────────────────────────────────────────────
# Step 370 — exec annotation
# ─────────────────────────────────────────────────────────────────────

class TestStep370Annotate:
    def test_allow_annotation(self) -> None:
        v = Verdict(decision="ALLOW", reason="", atv_id="x", step_traces={})
        step370_exec.annotate(v)
        traces = list(v.step_traces.values())
        assert any("PROCEED" in t for t in traces)

    def test_block_annotation(self) -> None:
        v = Verdict(decision="BLOCK", reason="", atv_id="x", step_traces={})
        step370_exec.annotate(v)
        traces = list(v.step_traces.values())
        assert any("SUPPRESS" in t for t in traces)

    def test_approval_annotation(self) -> None:
        v = Verdict(decision="REQUIRE_APPROVAL", reason="", atv_id="x", step_traces={})
        step370_exec.annotate(v)
        traces = list(v.step_traces.values())
        assert any("DEFER" in t for t in traces)
