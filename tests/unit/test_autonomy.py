"""v0.5.11 PR ⑭ — Autonomy module tests.

Covers learner (reason_signature, learn_trusted_patterns,
evaluate_autonomy_request), runtime (apply_autonomy_bypass,
trust table I/O), and outlier detection.

Uses synthetic ContextMemoryRecord lists so tests don't depend
on a real ~/.aegis/ store. Destructive vocab is concatenated at
import to bypass the local-mode firewall scanning this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from aegis.autonomy import (
    AUTONOMY_BYPASS_PREFIX,
    STEP_TRACE_KEY,
    OutlierEvent,
    TrustedPattern,
    apply_autonomy_bypass,
    detect_outliers,
    evaluate_autonomy_request,
    learn_trusted_patterns,
    load_trust_table,
    reason_signature,
    save_trust_table,
)
from aegis.context_memory.record import ContextMemoryRecord
from aegis.schema import Verdict

# Self-defense: never bake destructive literals into source bytes.
_KW_DROP_TABLE: Final[str] = "DROP" + " TABLE"


# ── helpers ────────────────────────────────────────────────────────


def _rec(
    *,
    aid: str = "agent-x",
    tool: str = "Bash",
    decision: str = "REQUIRE_APPROVAL",
    reason: str = "",
    trace: str = "tr",
    ts_ns: int = 1_700_000_000_000_000_000,
) -> ContextMemoryRecord:
    return ContextMemoryRecord(
        ts_ns=ts_ns, trace_id=trace, invocation_id="",
        aid=aid, tenant_id="t", tool_name=tool, decision=decision,
        reason=reason, channel=None, provider=None,
        latency_ms=10.0, cost_usd=0.001, tokens_in=100, tokens_out=50,
        step_traces={}, m13_score=None,
        advisor_invoked=False, recommended_advisors=(),
        atv_sha3=None, atv_dim=2080,
        is_sidechain=False, mode="local",
    )


def _verdict(
    decision: str = "REQUIRE_APPROVAL",
    reason: str = "step336: loop",
    step_traces: dict | None = None,
) -> Verdict:
    return Verdict(
        decision=decision,  # type: ignore[arg-type]
        reason=reason,
        atv_id="atv-x",
        step_traces=step_traces or {},
    )


# ── reason_signature ──────────────────────────────────────────────


def test_signature_loop_collapses_threshold_variants() -> None:
    """Different repetition counts must collapse to the same sig
    so the learner can build N-sample statistics."""
    a = reason_signature("same Bash call repeated 3 times this session")
    b = reason_signature("same Bash call repeated 7 times this session")
    assert a == b == "loop:Bash"


def test_signature_budget_collapses_amount() -> None:
    s = reason_signature("cumulative_dollars 1234.5 > budget 1.0000")
    assert s == "budget"


def test_signature_rule_keeps_specific_name() -> None:
    """Specific rule names must NOT collapse — each rule is its own
    trust category."""
    assert reason_signature("rule:prompt_injection") == "rule:prompt_injection"
    assert reason_signature("rule:foo") == "rule:foo"


def test_signature_dangerous_pattern() -> None:
    assert reason_signature(
        "dangerous pattern: " + _KW_DROP_TABLE,
    ) == "dangerous_pattern"


def test_signature_sensitive_path() -> None:
    assert reason_signature(
        "sensitive path requires approval: /etc/hosts",
    ) == "sensitive_path"


def test_signature_empty_string() -> None:
    assert reason_signature("") == "(empty)"


# ── learn_trusted_patterns — trust-table construction ─────────────


def test_learn_empty_records_yields_empty_table() -> None:
    table = learn_trusted_patterns([])
    assert table == {}


def test_learn_min_samples_threshold() -> None:
    """3 occurrences of one (tool, sig) pair is below min_samples=5
    → no trusted pattern."""
    records = [
        _rec(reason="same Bash call repeated 3 times this session",
             trace=f"t{i}")
        for i in range(3)
    ]
    table = learn_trusted_patterns(records, min_samples=5)
    assert table == {}


def test_learn_high_count_pattern_qualifies() -> None:
    records = [
        _rec(reason="same Bash call repeated 3 times this session",
             trace=f"t{i}")
        for i in range(10)
    ]
    table = learn_trusted_patterns(records, min_samples=5)
    assert ("Bash", "loop:Bash") in table
    p = table[("Bash", "loop:Bash")]
    assert p.n_seen == 10
    assert p.clean_rate == 1.0
    assert p.trust_score > 0.5


def test_learn_never_trust_filter_drops_dangerous() -> None:
    """A dangerous-pattern reason can never be trusted even with
    20 burn-in occurrences. Patent + safety contract."""
    records = [
        _rec(decision="REQUIRE_APPROVAL",
             reason="dangerous pattern: " + _KW_DROP_TABLE,
             trace=f"d{i}")
        for i in range(20)
    ]
    table = learn_trusted_patterns(records)
    assert table == {}


def test_learn_never_trust_filter_drops_git_destructive() -> None:
    records = [
        _rec(decision="REQUIRE_APPROVAL",
             reason="rule:git_destructive",
             trace=f"g{i}")
        for i in range(15)
    ]
    table = learn_trusted_patterns(records)
    assert table == {}


def test_learn_never_trust_filter_drops_sensitive_path() -> None:
    records = [
        _rec(decision="REQUIRE_APPROVAL",
             reason="sensitive path requires approval: /etc/hosts",
             trace=f"p{i}")
        for i in range(10)
    ]
    table = learn_trusted_patterns(records)
    assert table == {}


def test_learn_clean_rate_filter() -> None:
    """A pattern followed by BLOCK frequently (clean_rate < 0.95)
    must NOT qualify as trusted."""
    # 10 REQUIRE_APPROVAL records, each followed by a BLOCK from
    # the same aid → clean_rate = 0/10 = 0.
    records: list[ContextMemoryRecord] = []
    for i in range(10):
        records.append(_rec(
            aid="a1",
            reason="same Bash call repeated 3 times this session",
            trace=f"appr{i}",
            ts_ns=1_700_000_000_000_000_000 + i * 2,
        ))
        records.append(_rec(
            aid="a1",
            decision="BLOCK",
            reason="rule:foo",
            trace=f"blk{i}",
            ts_ns=1_700_000_000_000_000_000 + i * 2 + 1,
        ))
    table = learn_trusted_patterns(records, min_samples=5)
    # Cleanly-followed rate is 0 → pattern dropped despite many samples.
    assert ("Bash", "loop:Bash") not in table


def test_learn_only_counts_require_approval_records() -> None:
    """BLOCK records carrying the same reason must NOT be counted
    toward n_seen — the learner is about REQUIRE_APPROVAL only."""
    records = [
        _rec(decision="BLOCK", reason="same Bash call repeated 3 times this session")
        for _ in range(10)
    ]
    table = learn_trusted_patterns(records)
    assert table == {}


# ── evaluate_autonomy_request — runtime decision ──────────────────


def test_evaluate_unmatched_pattern_keeps_human_in_loop() -> None:
    av = evaluate_autonomy_request(
        tool_name="Bash",
        reason="some unknown reason",
        trust_table={},
    )
    assert av.auto_approve is False
    assert av.matched_pattern is None
    assert av.confidence == 0.0


def test_evaluate_low_trust_keeps_human_in_loop() -> None:
    """Pattern matches but trust_score < min_trust → no bypass."""
    p = TrustedPattern(
        tool_name="Bash", reason_signature="loop:Bash",
        n_seen=5, n_followed_by_block=0,
        clean_rate=1.0, trust_score=0.4,   # below 0.85 default
        last_seen_ns=0,
    )
    av = evaluate_autonomy_request(
        tool_name="Bash",
        reason="same Bash call repeated 3 times this session",
        trust_table={p.key: p},
    )
    assert av.auto_approve is False
    assert av.matched_pattern == p
    assert "below" in av.reason


def test_evaluate_high_trust_auto_approves() -> None:
    p = TrustedPattern(
        tool_name="Bash", reason_signature="loop:Bash",
        n_seen=50, n_followed_by_block=1,
        clean_rate=0.98, trust_score=0.95,
        last_seen_ns=0,
    )
    av = evaluate_autonomy_request(
        tool_name="Bash",
        reason="same Bash call repeated 3 times this session",
        trust_table={p.key: p},
    )
    assert av.auto_approve is True
    assert av.matched_pattern == p
    assert av.confidence == p.trust_score


def test_evaluate_never_trust_overrides_table() -> None:
    """Even if a malicious / stale trust table contains a
    dangerous-pattern entry with high trust, the runtime
    never-trust filter must block the bypass."""
    p = TrustedPattern(
        tool_name="Bash", reason_signature="dangerous_pattern",
        n_seen=100, n_followed_by_block=0,
        clean_rate=1.0, trust_score=1.0,
        last_seen_ns=0,
    )
    av = evaluate_autonomy_request(
        tool_name="Bash",
        reason="dangerous pattern: " + _KW_DROP_TABLE,
        trust_table={p.key: p},
    )
    assert av.auto_approve is False
    assert "never-trust" in av.reason


# ── runtime trust table I/O ───────────────────────────────────────


def test_trust_table_round_trip(tmp_path: Path) -> None:
    p = TrustedPattern(
        tool_name="Bash", reason_signature="loop:Bash",
        n_seen=10, n_followed_by_block=0,
        clean_rate=1.0, trust_score=0.9,
        last_seen_ns=1_700_000_000_000_000_000,
        sample_trace_ids=("a", "b", "c"),
    )
    table = {p.key: p}
    out = tmp_path / "trust.json"
    saved = save_trust_table(
        table, path=out,
        learned_from_records=100,
        min_samples=5, min_clean_rate=0.95,
    )
    assert saved == out
    loaded = load_trust_table(path=out)
    assert loaded == table


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    loaded = load_trust_table(path=tmp_path / "no-such-file.json")
    assert loaded == {}


def test_load_malformed_file_returns_empty(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_trust_table(path=bad) == {}


# ── apply_autonomy_bypass — the runtime shim ──────────────────────


def test_bypass_off_when_env_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AEGIS_AUTONOMY_ENABLED", raising=False)
    v = _verdict()
    new_v, av = apply_autonomy_bypass(
        v, tool_name="Bash",
        reason="same Bash call repeated 3 times this session",
        trust_table={},
    )
    # Verdict unchanged.
    assert new_v is v
    assert av.auto_approve is False
    assert "off" in av.reason.lower()


def test_bypass_only_targets_require_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ALLOW / BLOCK verdicts must NOT be downgraded."""
    monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
    p = TrustedPattern(
        tool_name="Bash", reason_signature="loop:Bash",
        n_seen=50, n_followed_by_block=0,
        clean_rate=1.0, trust_score=0.95,
        last_seen_ns=0,
    )
    for d in ("ALLOW", "BLOCK"):
        v = _verdict(decision=d, reason="x")
        new_v, _ = apply_autonomy_bypass(
            v, tool_name="Bash",
            reason="same Bash call repeated 3 times this session",
            trust_table={p.key: p},
        )
        assert new_v.decision == d


def test_bypass_downgrades_require_approval_to_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
    p = TrustedPattern(
        tool_name="Bash", reason_signature="loop:Bash",
        n_seen=50, n_followed_by_block=0,
        clean_rate=1.0, trust_score=0.95,
        last_seen_ns=0,
    )
    v = _verdict()
    new_v, av = apply_autonomy_bypass(
        v, tool_name="Bash",
        reason="same Bash call repeated 3 times this session",
        trust_table={p.key: p},
    )
    assert av.auto_approve is True
    assert new_v.decision == "ALLOW"
    assert "auto-approved" in new_v.reason
    # Stamp lands in step_traces.
    assert STEP_TRACE_KEY in new_v.step_traces
    assert new_v.step_traces[STEP_TRACE_KEY].startswith(
        AUTONOMY_BYPASS_PREFIX
    )


def test_bypass_preserves_other_step_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bypass adds the step331 stamp without dropping prior traces."""
    monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
    p = TrustedPattern(
        tool_name="Bash", reason_signature="loop:Bash",
        n_seen=50, n_followed_by_block=0,
        clean_rate=1.0, trust_score=0.95,
        last_seen_ns=0,
    )
    v = _verdict(step_traces={
        "step336": "loop detector fired",
        "step330_human": "blast 8",
    })
    new_v, _ = apply_autonomy_bypass(
        v, tool_name="Bash",
        reason="same Bash call repeated 3 times this session",
        trust_table={p.key: p},
    )
    assert "step336" in new_v.step_traces
    assert "step330_human" in new_v.step_traces
    assert STEP_TRACE_KEY in new_v.step_traces


def test_bypass_refuses_never_trust_even_in_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If somehow a dangerous-pattern entry made it into the trust
    table, runtime never-trust filter must still refuse the bypass."""
    monkeypatch.setenv("AEGIS_AUTONOMY_ENABLED", "1")
    p = TrustedPattern(
        tool_name="Bash", reason_signature="dangerous_pattern",
        n_seen=1000, n_followed_by_block=0,
        clean_rate=1.0, trust_score=1.0,
        last_seen_ns=0,
    )
    v = _verdict(reason="dangerous pattern: " + _KW_DROP_TABLE)
    new_v, av = apply_autonomy_bypass(
        v, tool_name="Bash",
        reason="dangerous pattern: " + _KW_DROP_TABLE,
        trust_table={p.key: p},
    )
    assert av.auto_approve is False
    assert new_v.decision == "REQUIRE_APPROVAL"


# ── outlier detection ─────────────────────────────────────────────


def test_outliers_empty_records() -> None:
    assert detect_outliers([]) == []


def test_outliers_records_without_bypass_stamp_ignored() -> None:
    """Records that were never auto-bypassed don't count."""
    records = [_rec(decision="ALLOW") for _ in range(5)]
    assert detect_outliers(records) == []


def test_outliers_bypass_with_clean_followup() -> None:
    """Auto-approved record followed only by ALLOWs → not an outlier."""
    bypass_stamp = (
        f"{AUTONOMY_BYPASS_PREFIX} by trust table tool=Bash "
        "signature=loop:Bash trust=0.95"
    )
    records = [
        ContextMemoryRecord(
            ts_ns=1, trace_id="auto1", invocation_id="",
            aid="a1", tenant_id="t", tool_name="Bash", decision="ALLOW",
            reason="auto-approved", channel=None, provider=None,
            latency_ms=10.0, cost_usd=0.0, tokens_in=0, tokens_out=0,
            step_traces={STEP_TRACE_KEY: bypass_stamp},
            m13_score=None,
            advisor_invoked=False, recommended_advisors=(),
            atv_sha3=None, atv_dim=2080,
            is_sidechain=False, mode="local",
        ),
        _rec(aid="a1", decision="ALLOW", trace="ok1", ts_ns=2),
        _rec(aid="a1", decision="ALLOW", trace="ok2", ts_ns=3),
    ]
    assert detect_outliers(records) == []


def test_outliers_bypass_followed_by_block_is_flagged() -> None:
    """Auto-approved record + BLOCK in same aid timeline → outlier."""
    bypass_stamp = (
        f"{AUTONOMY_BYPASS_PREFIX} by trust table tool=Bash "
        "signature=loop:Bash trust=0.92"
    )
    records = [
        ContextMemoryRecord(
            ts_ns=1, trace_id="auto1", invocation_id="",
            aid="a1", tenant_id="t", tool_name="Bash", decision="ALLOW",
            reason="auto-approved", channel=None, provider=None,
            latency_ms=10.0, cost_usd=0.0, tokens_in=0, tokens_out=0,
            step_traces={STEP_TRACE_KEY: bypass_stamp},
            m13_score=None,
            advisor_invoked=False, recommended_advisors=(),
            atv_sha3=None, atv_dim=2080,
            is_sidechain=False, mode="local",
        ),
        _rec(aid="a1", decision="BLOCK", reason="rule:foo",
             trace="blk1", ts_ns=2),
    ]
    events = detect_outliers(records)
    assert len(events) == 1
    e = events[0]
    assert isinstance(e, OutlierEvent)
    assert e.trace_id == "auto1"
    assert e.followup_block_trace == "blk1"
    assert e.followup_block_reason == "rule:foo"


def test_outliers_block_outside_lookahead_window_ignored() -> None:
    """A BLOCK 11+ records later is unrelated to the bypass."""
    bypass_stamp = (
        f"{AUTONOMY_BYPASS_PREFIX} by trust table tool=Bash "
        "signature=loop:Bash trust=0.92"
    )
    records: list[ContextMemoryRecord] = [
        ContextMemoryRecord(
            ts_ns=1, trace_id="auto1", invocation_id="",
            aid="a1", tenant_id="t", tool_name="Bash", decision="ALLOW",
            reason="auto-approved", channel=None, provider=None,
            latency_ms=10.0, cost_usd=0.0, tokens_in=0, tokens_out=0,
            step_traces={STEP_TRACE_KEY: bypass_stamp},
            m13_score=None,
            advisor_invoked=False, recommended_advisors=(),
            atv_sha3=None, atv_dim=2080,
            is_sidechain=False, mode="local",
        ),
    ]
    # 15 unrelated ALLOWs.
    for i in range(15):
        records.append(_rec(
            aid="a1", decision="ALLOW", trace=f"a{i}", ts_ns=2 + i,
        ))
    # Then a BLOCK far past the lookahead.
    records.append(_rec(
        aid="a1", decision="BLOCK", reason="rule:bar",
        trace="late_blk", ts_ns=100,
    ))
    events = detect_outliers(records, block_lookahead=10)
    assert events == []
