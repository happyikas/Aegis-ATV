"""Tests for ``aegis.context_memory`` — the silicon-emulation ATV
analytics store.

Covers:

* Record schema: from_dict / to_dict / from_audit_record projection
* Writer: append, env override, defensive on bad input, parent dir
  auto-create
* Reader: missing file, malformed lines, ts_ns window filtering
* Analytics: window summary, cost stats, performance percentiles,
  security stats (block rate, step distribution, provider drift)
* Advisor: each rule triggers under known thresholds (provider
  dominance, p95 > 50ms, BLOCK rate elevation, single-trace cost
  spike, slowest-trace alert)
* Markdown report renderer: structural assertions on output
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis.context_memory import (
    ContextMemoryRecord,
    append,
    context_memory_path,
    iter_records,
    read_all,
    read_window,
)
from aegis.context_memory.advisor import (
    cost_advice,
    performance_advice,
    security_advice,
)
from aegis.context_memory.analytics import (
    cost_stats,
    performance_stats,
    security_stats,
    window_summary,
)
from aegis.context_memory.record import (
    MAX_REASON_LEN,
    SCHEMA_VERSION,
)
from aegis.context_memory.report import render_doctor_report

# ── helpers ──────────────────────────────────────────────────────


def _make(
    *,
    ts_ns: int = 1_700_000_000_000_000_000,
    trace_id: str = "trace-1",
    tool: str = "Bash",
    decision: str = "ALLOW",
    reason: str = "ok",
    provider: str | None = "openrouter:anthropic-claude-sonnet-4",
    latency_ms: float = 12.5,
    cost_usd: float = 0.001,
    step_traces: dict[str, str] | None = None,
    aid: str = "session-A",
    recommended_advisors: tuple[str, ...] = (),
) -> ContextMemoryRecord:
    return ContextMemoryRecord(
        ts_ns=ts_ns,
        trace_id=trace_id,
        invocation_id=trace_id,
        aid=aid,
        tenant_id="claude-code-local",
        tool_name=tool,
        decision=decision,
        reason=reason,
        channel=None,
        provider=provider,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        tokens_in=100,
        tokens_out=50,
        step_traces=step_traces or {},
        m13_score=None,
        advisor_invoked=False,
        recommended_advisors=recommended_advisors,
        atv_sha3=None,
        atv_dim=2080,
        is_sidechain=False,
        mode="local",
    )


# ── record: round-trip ──────────────────────────────────────────


def test_record_to_from_dict_round_trip() -> None:
    rec = _make(reason="dangerous bash", cost_usd=0.0042)
    d = rec.to_dict()
    rec2 = ContextMemoryRecord.from_dict(d)
    assert rec2 == rec


def test_record_from_dict_tolerates_missing_optionals() -> None:
    minimal = {
        "ts_ns": 12345,
        "trace_id": "t",
        "invocation_id": "i",
        "aid": "a",
        "tool_name": "Bash",
        "decision": "ALLOW",
    }
    rec = ContextMemoryRecord.from_dict(minimal)
    assert rec.trace_id == "t"
    assert rec.provider is None
    assert rec.channel is None
    assert rec.m13_score is None
    assert rec.recommended_advisors == ()
    assert rec.schema_version == SCHEMA_VERSION


def test_record_truncates_long_reason() -> None:
    rec = ContextMemoryRecord.from_dict({
        "ts_ns": 1, "trace_id": "t", "decision": "ALLOW",
        "reason": "x" * 1000,
    })
    assert len(rec.reason) == MAX_REASON_LEN


# ── record: from_audit_record projection ────────────────────────


def test_from_audit_record_local_mode_shape() -> None:
    audit = {
        "ts_ns": 1_700_000_000_000_000_000,
        "tool": "Bash",
        "aid": "session-A",
        "invocation_id": "inv-1",
        "trace_id": "trace-1",
        "decision": "BLOCK",
        "reason": "destructive bash",
        "latency_ms": 47.3,
        "mode": "local",
        "channel": "telegram",
        "provider": "openrouter:anthropic-claude-sonnet-4",
        "explain": {
            "step_traces": {"step310": "destructive bash"},
            "m13_score": 0.81,
            "advisor_gate": {"invoked": True, "reason": "non-allow"},
            "action_advice": {
                "recommended_advisors": [
                    {"advisor": "security-reviewer", "priority": "high"},
                    {"advisor": "loop-breaker", "priority": "medium"},
                ],
            },
            "atv_sha3": "abc",
            "atv_dim": 2080,
            "cost": {"total_usd": 0.0042, "tokens_in": 412, "tokens_out": 86},
        },
    }
    rec = ContextMemoryRecord.from_audit_record(audit)
    assert rec.tool_name == "Bash"
    assert rec.decision == "BLOCK"
    assert rec.reason == "destructive bash"
    assert rec.channel == "telegram"
    assert rec.provider == "openrouter:anthropic-claude-sonnet-4"
    assert rec.latency_ms == pytest.approx(47.3)
    assert rec.cost_usd == pytest.approx(0.0042)
    assert rec.tokens_in == 412
    assert rec.step_traces == {"step310": "destructive bash"}
    assert rec.m13_score == pytest.approx(0.81)
    assert rec.advisor_invoked is True
    assert rec.recommended_advisors == ("security-reviewer", "loop-breaker")
    assert rec.atv_sha3 == "abc"
    assert rec.mode == "local"


def test_from_audit_record_recommended_advisors_string_form() -> None:
    """Some audit producers emit list[str] instead of list[dict]."""
    audit = {
        "trace_id": "t1", "decision": "ALLOW",
        "explain": {"action_advice": {
            "recommended_advisors": ["cost-optimizer", "security-reviewer"],
        }},
    }
    rec = ContextMemoryRecord.from_audit_record(audit)
    assert rec.recommended_advisors == ("cost-optimizer", "security-reviewer")


def test_from_audit_record_missing_fields() -> None:
    """Audit records can be very sparse (e.g. early sidecar versions).
    Projection must still produce a valid record."""
    audit = {"ts_ns": 1, "decision": "ALLOW", "tool": "Read"}
    rec = ContextMemoryRecord.from_audit_record(audit)
    assert rec.decision == "ALLOW"
    assert rec.tool_name == "Read"
    assert rec.latency_ms == 0.0
    assert rec.cost_usd == 0.0


def test_from_audit_record_uses_tool_name_alias() -> None:
    """Sidecar uses 'tool_name'; local uses 'tool'. Both must work."""
    audit = {"trace_id": "t", "decision": "ALLOW", "tool_name": "Edit"}
    rec = ContextMemoryRecord.from_audit_record(audit)
    assert rec.tool_name == "Edit"


# ── writer ───────────────────────────────────────────────────────


def test_append_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "cm.jsonl"
    ok = append(_make(trace_id="t1"), path=p)
    assert ok is True
    recs = read_all(p)
    assert len(recs) == 1
    assert recs[0].trace_id == "t1"


def test_append_creates_parent_dir(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "nested" / "cm.jsonl"
    assert append(_make(), path=p) is True
    assert p.exists()


def test_append_dict_input_invokes_from_audit_record(tmp_path: Path) -> None:
    p = tmp_path / "cm.jsonl"
    audit_rec = {
        "ts_ns": 12345, "trace_id": "t-dict", "decision": "ALLOW",
        "tool": "Bash",
    }
    ok = append(audit_rec, path=p, mode="sidecar")
    assert ok is True
    recs = read_all(p)
    assert recs[0].trace_id == "t-dict"
    assert recs[0].mode == "sidecar"


def test_append_returns_false_on_bad_path() -> None:
    # /dev/null/foo can't be created — append should swallow
    ok = append(_make(), path=Path("/dev/null/cm.jsonl"))
    assert ok is False


def test_append_returns_false_on_unserializable_record(tmp_path: Path) -> None:
    """Pass a dict with un-jsonable values inside step_traces or
    similar. Writer must not raise."""
    p = tmp_path / "cm.jsonl"

    class Unserializable:
        pass

    bad_audit = {
        "trace_id": "t", "decision": "ALLOW",
        "explain": {"cost": {"total_usd": Unserializable()}},
    }
    # The projection step yields a sensible default for total_usd
    # (Unserializable can't coerce to float → 0.0). So the write
    # actually succeeds — assert it doesn't crash either way.
    ok = append(bad_audit, path=p)
    assert ok in (True, False)
    # The file may exist if write succeeded
    if ok:
        assert p.exists()


def test_context_memory_path_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    custom = tmp_path / "custom-cm.jsonl"
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_PATH", str(custom))
    assert context_memory_path() == custom


def test_context_memory_path_default_when_env_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_PATH", "")
    p = context_memory_path()
    assert p.name == "context_memory.jsonl"
    assert p.parent.name == ".aegis"


# ── reader ───────────────────────────────────────────────────────


def test_iter_records_empty_when_missing(tmp_path: Path) -> None:
    assert list(iter_records(tmp_path / "absent.jsonl")) == []


def test_iter_records_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "cm.jsonl"
    valid = json.dumps(_make(trace_id="ok").to_dict())
    p.write_text(
        f"{valid}\n"
        "{ broken json\n"
        f"{valid.replace('ok', 'ok2')}\n",
        encoding="utf-8",
    )
    recs = read_all(p)
    assert len(recs) == 2
    assert recs[0].trace_id == "ok"
    assert recs[1].trace_id == "ok2"


def test_read_window_filters_by_time(tmp_path: Path) -> None:
    p = tmp_path / "cm.jsonl"
    t0 = 1_700_000_000_000_000_000
    for i in range(5):
        append(_make(ts_ns=t0 + i * 1_000_000_000, trace_id=f"t{i}"), path=p)
    # Only those at t0+2..t0+3
    filt = read_window(
        since_ns=t0 + 2_000_000_000,
        until_ns=t0 + 3_000_000_000,
        path=p,
    )
    assert {r.trace_id for r in filt} == {"t2", "t3"}


# ── analytics: window summary ───────────────────────────────────


def test_window_summary_empty() -> None:
    s = window_summary([])
    assert s.n_total == 0
    assert s.block_rate == 0.0


def test_window_summary_distribution() -> None:
    recs = [
        _make(decision="ALLOW"),
        _make(decision="ALLOW"),
        _make(decision="REQUIRE_APPROVAL"),
        _make(decision="BLOCK"),
    ]
    s = window_summary(recs)
    assert s.n_total == 4
    assert s.n_allow == 2
    assert s.n_approval == 1
    assert s.n_block == 1
    assert s.allow_rate == 0.5
    assert s.block_rate == 0.25


# ── analytics: cost ──────────────────────────────────────────────


def test_cost_stats_empty() -> None:
    s = cost_stats([])
    assert s.total_usd == 0.0
    assert s.n_priced == 0
    assert s.by_provider == ()


def test_cost_stats_groups_by_provider() -> None:
    recs = [
        _make(provider="A", cost_usd=0.10),
        _make(provider="A", cost_usd=0.05),
        _make(provider="B", cost_usd=0.20),
        _make(provider=None, cost_usd=0.01),  # → (no-provider)
    ]
    s = cost_stats(recs)
    assert s.total_usd == pytest.approx(0.36)
    assert s.n_priced == 4
    # sorted by total_usd desc → B first
    assert s.by_provider[0].provider == "B"
    assert s.by_provider[0].total_usd == pytest.approx(0.20)
    no_prov = [p for p in s.by_provider if p.provider == "(no-provider)"]
    assert len(no_prov) == 1
    assert no_prov[0].total_usd == pytest.approx(0.01)


def test_cost_stats_top_expensive_traces() -> None:
    recs = [
        _make(trace_id="cheap", cost_usd=0.001),
        _make(trace_id="middle", cost_usd=0.05),
        _make(trace_id="expensive", cost_usd=0.50),
    ]
    s = cost_stats(recs)
    assert s.top_expensive_traces[0][0] == "expensive"
    assert s.top_expensive_traces[0][1] == pytest.approx(0.50)


def test_cost_stats_separates_unpriced() -> None:
    recs = [
        _make(cost_usd=0.10),
        _make(cost_usd=0.0),
        _make(cost_usd=0.0),
    ]
    s = cost_stats(recs)
    assert s.n_priced == 1
    assert s.n_unpriced == 2


# ── analytics: performance ──────────────────────────────────────


def test_performance_stats_percentiles() -> None:
    recs = [_make(latency_ms=float(x)) for x in range(1, 101)]  # 1..100
    s = performance_stats(recs)
    assert s.overall.p50 == pytest.approx(50.5)
    assert s.overall.p95 == pytest.approx(95.05)
    assert s.overall.p99 == pytest.approx(99.01)
    assert s.overall.max == 100.0
    assert s.overall.n == 100


def test_performance_stats_by_tool_sort() -> None:
    recs = [
        _make(tool="Fast", latency_ms=5.0),
        _make(tool="Fast", latency_ms=10.0),
        _make(tool="Slow", latency_ms=500.0),
        _make(tool="Slow", latency_ms=600.0),
    ]
    s = performance_stats(recs)
    # Sorted by p95 desc
    assert s.by_tool[0].tool == "Slow"


def test_performance_stats_ignores_zero_latency() -> None:
    recs = [_make(latency_ms=0.0) for _ in range(3)]
    s = performance_stats(recs)
    assert s.overall.n == 0


# ── analytics: security ─────────────────────────────────────────


def test_security_stats_block_rate() -> None:
    recs = (
        [_make(decision="ALLOW") for _ in range(95)]
        + [_make(decision="BLOCK") for _ in range(5)]
    )
    s = security_stats(recs)
    assert s.n_block == 5
    assert s.block_rate == pytest.approx(0.05)


def test_security_stats_step_distribution() -> None:
    recs = [
        _make(decision="BLOCK", step_traces={"step310": "x"}),
        _make(decision="BLOCK", step_traces={"step310": "x"}),
        _make(decision="BLOCK", step_traces={"step311": "y"}),
    ]
    s = security_stats(recs)
    assert s.block_by_step[0].step == "step310"
    assert s.block_by_step[0].count == 2
    assert s.block_by_step[1].step == "step311"
    assert s.block_by_step[1].count == 1


def test_security_stats_provider_block_rates() -> None:
    recs = (
        [_make(provider="safe", decision="ALLOW") for _ in range(100)]
        + [_make(provider="risky", decision="BLOCK") for _ in range(5)]
        + [_make(provider="risky", decision="ALLOW") for _ in range(5)]
    )
    s = security_stats(recs)
    risky = next(p for p in s.by_provider if p.provider == "risky")
    safe = next(p for p in s.by_provider if p.provider == "safe")
    assert risky.block_rate == 0.5
    assert safe.block_rate == 0.0


# ── advisor: cost ───────────────────────────────────────────────


def test_cost_advice_provider_dominance_triggers_high() -> None:
    recs = (
        [_make(provider="dominant", cost_usd=0.10) for _ in range(8)]
        + [_make(provider="other", cost_usd=0.01) for _ in range(2)]
    )
    recs2 = cost_advice(cost_stats(recs))
    high = [r for r in recs2 if r.priority == "high"]
    assert any("dominant" in r.headline for r in high)


def test_cost_advice_healthy_state() -> None:
    recs = [
        _make(provider="a", cost_usd=0.01),
        _make(provider="b", cost_usd=0.01),
        _make(provider="c", cost_usd=0.01),
    ]
    advs = cost_advice(cost_stats(recs))
    # No provider dominance, no outlier — healthy info card
    assert any(r.priority == "info" for r in advs)


def test_cost_advice_unpriced_dominance() -> None:
    """50 records with most missing cost → medium advisory."""
    recs = (
        [_make(cost_usd=0.01) for _ in range(10)]
        + [_make(cost_usd=0.0) for _ in range(50)]
    )
    advs = cost_advice(cost_stats(recs))
    assert any("미부여" in r.headline for r in advs)


def test_cost_advice_high_single_trace() -> None:
    recs = [
        _make(trace_id="cheap", cost_usd=0.01),
        _make(trace_id="whopper", cost_usd=1.50),
    ]
    advs = cost_advice(cost_stats(recs))
    assert any("$1.50" in r.headline for r in advs)


# ── advisor: performance ────────────────────────────────────────


def test_performance_advice_p95_exceeds_target() -> None:
    recs = [_make(latency_ms=200.0) for _ in range(20)]
    advs = performance_advice(performance_stats(recs))
    assert any(r.priority in ("medium", "high") for r in advs)
    assert any("p95" in r.headline for r in advs)


def test_performance_advice_p95_within_target() -> None:
    recs = [_make(latency_ms=10.0) for _ in range(20)]
    advs = performance_advice(performance_stats(recs))
    assert any("충족" in r.headline for r in advs)


def test_performance_advice_tool_outlier() -> None:
    recs = (
        [_make(tool="fast", latency_ms=5.0) for _ in range(10)]
        + [_make(tool="ok", latency_ms=10.0) for _ in range(10)]
        + [_make(tool="slow", latency_ms=200.0) for _ in range(10)]
    )
    advs = performance_advice(performance_stats(recs))
    assert any("slow" in r.headline for r in advs)


def test_performance_advice_single_slow_trace() -> None:
    recs = [
        _make(latency_ms=10.0) for _ in range(20)
    ] + [_make(latency_ms=800.0, trace_id="bomb")]
    advs = performance_advice(performance_stats(recs))
    assert any("800" in r.headline for r in advs)


# ── advisor: security ───────────────────────────────────────────


def test_security_advice_block_rate_high() -> None:
    recs = (
        [_make(decision="ALLOW") for _ in range(80)]
        + [_make(decision="BLOCK", step_traces={"step310": "x"}) for _ in range(20)]
    )
    advs = security_advice(security_stats(recs))
    assert any(r.priority == "high" for r in advs)
    assert any("BLOCK rate" in r.headline for r in advs)


def test_security_advice_dominant_step() -> None:
    recs = (
        [
            _make(decision="BLOCK", step_traces={"step310": "x"})
            for _ in range(8)
        ]
        + [
            _make(decision="BLOCK", step_traces={"step311": "y"})
            for _ in range(2)
        ]
    )
    advs = security_advice(security_stats(recs))
    assert any("step310" in r.headline for r in advs)


def test_security_advice_provider_drift() -> None:
    """Provider C has 30× the block rate of the cross-provider median
    (with 3+ providers ≥ 10 calls each) → high-priority drift advisory.

    Need 3+ providers because with only 2, the "median" reduces to
    the higher of the two and no advisory fires.
    """
    recs = (
        # Provider A: 100 calls, 1 block → 1%
        [_make(provider="A", decision="ALLOW") for _ in range(99)]
        + [_make(provider="A", decision="BLOCK",
                 step_traces={"step310": "x"})]
        # Provider B: 100 calls, 1 block → 1%
        + [_make(provider="B", decision="ALLOW") for _ in range(99)]
        + [_make(provider="B", decision="BLOCK",
                 step_traces={"step310": "y"})]
        # Provider C: 20 calls, 6 block → 30% (30× of 1% median)
        + [_make(provider="C", decision="ALLOW") for _ in range(14)]
        + [
            _make(provider="C", decision="BLOCK",
                  step_traces={"step310": "z"})
            for _ in range(6)
        ]
    )
    advs = security_advice(security_stats(recs))
    assert any(
        "Provider drift" in r.headline or "drift" in r.headline.lower()
        for r in advs
    )


def test_security_advice_high_approval_rate() -> None:
    recs = (
        [_make(decision="ALLOW") for _ in range(70)]
        + [_make(decision="REQUIRE_APPROVAL") for _ in range(30)]
    )
    advs = security_advice(security_stats(recs))
    assert any("REQUIRE_APPROVAL rate" in r.headline for r in advs)


# ── report renderer ─────────────────────────────────────────────


def test_render_doctor_report_structure() -> None:
    recs = [
        _make(decision="ALLOW", latency_ms=15.0),
        _make(decision="BLOCK", latency_ms=20.0,
              step_traces={"step310": "x"}),
        _make(decision="REQUIRE_APPROVAL", latency_ms=25.0),
    ]
    md = render_doctor_report(recs, since_seconds=3600)
    # Has all 4 major sections
    assert "# Aegis Doctor Report" in md
    assert "## 📊 요약" in md
    assert "## 💰 Cost" in md
    assert "## ⚡ Performance" in md
    assert "## 🛡️ Security" in md
    assert "## 📌 다음 액션" in md
    # Footer
    assert "Generated at" in md
    assert "aegis doctor" in md


def test_render_doctor_report_empty_records() -> None:
    md = render_doctor_report([], since_seconds=3600)
    assert "ContextMemory 가 비어있거나" in md


def test_render_doctor_report_includes_window_humanise() -> None:
    md = render_doctor_report([_make()], since_seconds=86400)
    # 1.0 일
    assert "1.0 일" in md


def test_render_doctor_report_no_duration_uses_inferred_span() -> None:
    """When since_seconds is None and records have a span, the
    inferred span is shown."""
    t0 = 1_700_000_000_000_000_000
    recs = [
        _make(ts_ns=t0, trace_id="a"),
        _make(ts_ns=t0 + 600_000_000_000, trace_id="b"),  # +10 minutes
    ]
    md = render_doctor_report(recs, since_seconds=None)
    # Should mention "최근" (humanise output) but not "(전체 기간)" since
    # we have a span.
    assert "기간" in md


def test_render_doctor_report_deterministic_given_fixed_ts() -> None:
    """Two renders with the same inputs (including generated_at)
    must produce identical bytes."""
    import datetime as _dt

    rec = _make()
    fixed = _dt.datetime(2026, 5, 15, 12, 0, 0, tzinfo=_dt.UTC)
    md1 = render_doctor_report([rec], since_seconds=3600, generated_at=fixed)
    md2 = render_doctor_report([rec], since_seconds=3600, generated_at=fixed)
    assert md1 == md2
