"""Unit tests for Gap C (#146) — per-(aid × provider) Coach burn-in.

Three things to verify:

1. **Schema migration** — ``LayerKey`` accepts a ``provider`` field;
   ``as_str()`` is backward compatible with old (no-provider) keys.
2. **3-tuple slot allocation** — ``observe()`` creates separate L5
   slots per provider on the same aid; old records (no provider) get
   their own slot keyed by the legacy 4-part string.
3. **Live drift advisor** — ``provider_drift_for_aid()`` returns the
   right structure for rate divergence and zero-block-outlier cases.
"""

from __future__ import annotations

import time

from aegis.burnin import BurnInController, LayerKey
from aegis.burnin.phases import PhaseMetrics
from aegis.schema import ATVHeader, ATVInput, Verdict


def _inp(
    *, tenant: str = "t", role: str = "r", aid: str = "a",
    provider: str | None = None,
) -> ATVInput:
    header = ATVHeader(
        trace_id="t", span_id="s", tenant_id=tenant, aid=aid,
        timestamp_ns=time.time_ns(),
        provider=provider,
    )
    return ATVInput(
        header=header,
        plan_text="plan",
        tool_name="Bash",
        tool_args_json='{}',
        role_id=role,
    )


def _verdict(decision: str = "ALLOW") -> Verdict:
    return Verdict(
        decision=decision, reason="", atv_id="a",
        signature="s", confidence=1.0, step_traces={},
    )


# ── LayerKey schema migration ───────────────────────────────────


def test_layerkey_default_provider_is_none() -> None:
    k = LayerKey("L5", tenant_id="t", aid="a")
    assert k.provider is None


def test_layerkey_provider_field_in_as_str() -> None:
    k = LayerKey(
        "L5", tenant_id="t", role_id="r", aid="a",
        provider="anthropic",
    )
    s = k.as_str()
    # Old 4-part format `L5:t:r:a` is preserved as a *prefix*; the
    # provider tag is appended last so existing slots are not crossed-up.
    assert s == "L5:t:r:a:prov=anthropic"


def test_layerkey_no_provider_uses_legacy_format() -> None:
    """Without provider, the key is identical to the pre-Gap C form."""
    k = LayerKey("L5", tenant_id="t", role_id="r", aid="a")
    assert k.as_str() == "L5:t:r:a"


def test_layerkey_provider_does_not_affect_l1_through_l4() -> None:
    """Provider field is per-instance only; the patent's L1-L4 layers
    don't carry it. (We don't enforce this in the dataclass — it's
    documentation. The mapping function in ``_layer_keys_for`` is
    what enforces it; covered below.)"""
    # Even if a caller passes provider on L4, as_str renders it,
    # because LayerKey is dumb. The constraint lives in the mapper.
    k = LayerKey("L4", tenant_id="t", role_id="r", provider="claude")
    assert "prov=claude" in k.as_str()


# ── Slot allocation across providers ────────────────────────────


def test_two_providers_same_aid_get_two_l5_slots() -> None:
    c = BurnInController()
    c.observe(_inp(provider="anthropic"), _verdict("ALLOW"))
    c.observe(_inp(provider="openai"), _verdict("ALLOW"))

    keys = list(c._slots.keys())
    assert "L5:t:r:a:prov=anthropic" in keys
    assert "L5:t:r:a:prov=openai" in keys
    # Same agent, two providers → two slots — neither displaces
    # the other.
    assert sum(1 for k in keys if k.startswith("L5:t:r:a")) == 2


def test_no_provider_record_uses_legacy_4part_slot() -> None:
    """Backward compat: a record with no provider field bucket under
    the old 4-part key. Observed alongside provider'd records, the
    legacy slot is independent."""
    c = BurnInController()
    c.observe(_inp(provider=None), _verdict("ALLOW"))
    c.observe(_inp(provider="anthropic"), _verdict("ALLOW"))

    keys = sorted(c._slots.keys())
    assert "L5:t:r:a" in keys             # legacy
    assert "L5:t:r:a:prov=anthropic" in keys


def test_observe_increments_decision_and_block_counts() -> None:
    c = BurnInController()
    c.observe(_inp(provider="x"), _verdict("BLOCK"))
    c.observe(_inp(provider="x"), _verdict("ALLOW"))
    c.observe(_inp(provider="x"), _verdict("REQUIRE_APPROVAL"))

    slot = c._slots["L5:t:r:a:prov=x"]
    m = slot.state.metrics
    assert m.samples == 3
    assert m.decision_count == 3
    assert m.block_count == 1
    assert m.block_rate == 1 / 3


def test_phase_metrics_block_rate_zero_when_no_decisions() -> None:
    m = PhaseMetrics()
    assert m.block_rate == 0.0


# ── provider_drift_for_aid ──────────────────────────────────────


def test_drift_silent_when_one_provider_only() -> None:
    c = BurnInController()
    for _ in range(20):
        c.observe(_inp(provider="anthropic"), _verdict("BLOCK"))
    drifts = c.provider_drift_for_aid("t", "r", "a")
    assert drifts == []


def test_drift_silent_below_min_samples() -> None:
    """min_samples=5 default — providers with <5 decisions don't
    qualify for the comparison."""
    c = BurnInController()
    # anthropic: 3 calls, 3 BLOCKs (100%) — below threshold
    for _ in range(3):
        c.observe(_inp(provider="anthropic"), _verdict("BLOCK"))
    # openai: 5 calls, 1 BLOCK (20%)
    c.observe(_inp(provider="openai"), _verdict("BLOCK"))
    for _ in range(4):
        c.observe(_inp(provider="openai"), _verdict("ALLOW"))
    drifts = c.provider_drift_for_aid("t", "r", "a")
    assert drifts == []


def test_drift_silent_below_divergence_multiplier() -> None:
    c = BurnInController()
    # anthropic: 2 BLOCKs / 10 = 20%
    for _ in range(2):
        c.observe(_inp(provider="anthropic"), _verdict("BLOCK"))
    for _ in range(8):
        c.observe(_inp(provider="anthropic"), _verdict("ALLOW"))
    # openai: 1 BLOCK / 10 = 10% (only 2x — under default 3x)
    c.observe(_inp(provider="openai"), _verdict("BLOCK"))
    for _ in range(9):
        c.observe(_inp(provider="openai"), _verdict("ALLOW"))
    drifts = c.provider_drift_for_aid("t", "r", "a")
    assert drifts == []


def test_drift_fires_on_5x_divergence() -> None:
    c = BurnInController()
    # anthropic: 5/10 = 50% BLOCK
    for _ in range(5):
        c.observe(_inp(provider="anthropic"), _verdict("BLOCK"))
    for _ in range(5):
        c.observe(_inp(provider="anthropic"), _verdict("ALLOW"))
    # openai: 1/10 = 10% BLOCK
    c.observe(_inp(provider="openai"), _verdict("BLOCK"))
    for _ in range(9):
        c.observe(_inp(provider="openai"), _verdict("ALLOW"))
    drifts = c.provider_drift_for_aid("t", "r", "a")
    assert len(drifts) == 1
    d = drifts[0]
    assert d["aid"] == "a"
    assert d["max_provider"] == "anthropic"
    assert d["min_provider"] == "openai"
    assert d["max_rate"] == 0.5
    assert d["min_rate"] == 0.1
    assert d["ratio"] == 5.0
    assert d["kind"] == "rate-divergence"


def test_drift_zero_block_outlier_flagged() -> None:
    """When peer providers BLOCK at >0% and one provider has 0% with
    enough samples, the 0%-side is flagged."""
    c = BurnInController()
    # anthropic: 3/10 = 30% BLOCK
    for _ in range(3):
        c.observe(_inp(provider="anthropic"), _verdict("BLOCK"))
    for _ in range(7):
        c.observe(_inp(provider="anthropic"), _verdict("ALLOW"))
    # mystery-provider: 0/10 = 0% BLOCK (but with enough samples)
    for _ in range(10):
        c.observe(_inp(provider="mystery"), _verdict("ALLOW"))
    drifts = c.provider_drift_for_aid("t", "r", "a")
    assert len(drifts) == 1
    d = drifts[0]
    assert d["kind"] == "zero-block-outlier"
    assert d["min_rate"] == 0.0
    assert d["max_rate"] == 0.3


def test_drift_excludes_no_provider_bucket() -> None:
    """Old records without provider should not participate in the
    cross-provider comparison."""
    c = BurnInController()
    # anthropic: 5/10 = 50%
    for _ in range(5):
        c.observe(_inp(provider="anthropic"), _verdict("BLOCK"))
    for _ in range(5):
        c.observe(_inp(provider="anthropic"), _verdict("ALLOW"))
    # legacy (no provider): 0/10 = 0% — must be excluded
    for _ in range(10):
        c.observe(_inp(provider=None), _verdict("ALLOW"))
    # No second real provider; comparison is impossible.
    drifts = c.provider_drift_for_aid("t", "r", "a")
    assert drifts == []


def test_drift_silent_when_all_providers_at_zero() -> None:
    c = BurnInController()
    for _ in range(10):
        c.observe(_inp(provider="anthropic"), _verdict("ALLOW"))
    for _ in range(10):
        c.observe(_inp(provider="openai"), _verdict("ALLOW"))
    drifts = c.provider_drift_for_aid("t", "r", "a")
    assert drifts == []


def test_drift_scoped_per_aid() -> None:
    """A different aid's drift state must not pollute aid='a'."""
    c = BurnInController()
    # aid='a' on anthropic — 50% BLOCK
    for _ in range(5):
        c.observe(_inp(aid="a", provider="anthropic"), _verdict("BLOCK"))
    for _ in range(5):
        c.observe(_inp(aid="a", provider="anthropic"), _verdict("ALLOW"))
    # aid='b' on openai — 10% BLOCK (one provider, would be silent)
    c.observe(_inp(aid="b", provider="openai"), _verdict("BLOCK"))
    for _ in range(9):
        c.observe(_inp(aid="b", provider="openai"), _verdict("ALLOW"))
    # aid='a' has only one real provider here — no comparison possible
    assert c.provider_drift_for_aid("t", "r", "a") == []
    assert c.provider_drift_for_aid("t", "r", "b") == []


# ── status_by_provider ──────────────────────────────────────────


def test_status_by_provider_groups_l5_only() -> None:
    c = BurnInController()
    c.observe(_inp(provider="anthropic"), _verdict("BLOCK"))
    c.observe(_inp(provider="openai"), _verdict("ALLOW"))
    c.observe(_inp(provider=None), _verdict("ALLOW"))

    by_prov = c.status_by_provider()
    # Each provider key is present.
    assert "anthropic" in by_prov
    assert "openai" in by_prov
    assert "(no-provider)" in by_prov
    # Output is sorted alphabetically.
    assert list(by_prov.keys()) == sorted(by_prov.keys())


def test_status_by_provider_records_block_rate() -> None:
    c = BurnInController()
    for _ in range(4):
        c.observe(_inp(provider="anthropic"), _verdict("BLOCK"))
    for _ in range(6):
        c.observe(_inp(provider="anthropic"), _verdict("ALLOW"))
    by_prov = c.status_by_provider()
    rows = by_prov["anthropic"]
    assert len(rows) == 1
    assert rows[0]["block_rate"] == 0.4
    assert rows[0]["decision_count"] == 10
    assert rows[0]["block_count"] == 4


def test_status_by_provider_excludes_l1_through_l4() -> None:
    """L1-L4 don't have provider scope — they shouldn't show up here."""
    c = BurnInController()
    c.observe(_inp(provider="anthropic"), _verdict("ALLOW"))
    by_prov = c.status_by_provider()
    # Only L5 slots are returned → exactly one entry under "anthropic"
    # and no L1-L4 leakage. Keys are providers, not layer names.
    assert "L1" not in by_prov
    assert "L2" not in by_prov
    assert "L3" not in by_prov
    assert "L4" not in by_prov


# ── status() includes the new fields ────────────────────────────


def test_status_row_has_provider_block_count_decision_count() -> None:
    c = BurnInController()
    c.observe(_inp(provider="anthropic"), _verdict("BLOCK"))

    rows = c.status()["layers"]
    l5_row = next(r for r in rows if r["layer"] == "L5")
    assert l5_row["provider"] == "anthropic"
    assert l5_row["decision_count"] == 1
    assert l5_row["block_count"] == 1
    assert l5_row["block_rate"] == 1.0


# ── live advisor wiring (evaluate.py) ───────────────────────────


def test_evaluate_surfaces_provider_drift_in_step_traces(
    aegis_app: object,
) -> None:
    """When the controller has accumulated drift, an evaluate() call
    surfaces the divergence in ``verdict.step_traces``.

    Uses the shared ``aegis_app`` fixture so the FastAPI app is fully
    wired (signing key, audit DB, JsonlStore, BurnInController). We
    pre-populate the controller via direct ``observe`` calls — these
    don't go through the firewall, so they're cheap — then issue one
    real ``POST /evaluate`` and inspect the verdict's step_traces.
    """
    from fastapi.testclient import TestClient

    app = aegis_app  # type: ignore[assignment]
    # Reach into the app state to pre-populate the controller. This
    # mirrors what real traffic would build up over time.
    controller: BurnInController = app.state.burnin_controller  # type: ignore[attr-defined]
    for _ in range(5):
        controller.observe(_inp(provider="anthropic"), _verdict("BLOCK"))
    for _ in range(5):
        controller.observe(_inp(provider="anthropic"), _verdict("ALLOW"))
    controller.observe(_inp(provider="openai"), _verdict("BLOCK"))
    for _ in range(9):
        controller.observe(_inp(provider="openai"), _verdict("ALLOW"))
    assert controller.provider_drift_for_aid("t", "r", "a") != []

    # Issue one evaluate() with the same aid + a provider, and verify
    # the advisor signal lands in step_traces.
    client = TestClient(app)  # type: ignore[arg-type]
    payload = {
        "header": {
            "trace_id": "trace-drift",
            "span_id": "span-drift",
            "tenant_id": "t",
            "aid": "a",
            "timestamp_ns": int(time.time_ns()),
            "provider": "anthropic",
        },
        "tool_name": "Bash",
        "tool_args_json": "{}",
        "role_id": "r",
    }
    res = client.post("/evaluate", json=payload)
    assert res.status_code == 200, res.text
    body = res.json()
    traces = body.get("step_traces", {})
    assert "aegis.coach.provider_drift" in traces, (
        f"expected provider_drift trace; got keys: {list(traces.keys())}"
    )
    trace = traces["aegis.coach.provider_drift"]
    assert "aid=a" in trace
    assert "ratio=" in trace


def test_evaluate_drift_advisor_silent_when_no_divergence(
    aegis_app: object,
) -> None:
    """Without divergence the step_traces key is absent (clean output)."""
    from fastapi.testclient import TestClient

    app = aegis_app  # type: ignore[assignment]
    client = TestClient(app)  # type: ignore[arg-type]
    payload = {
        "header": {
            "trace_id": "trace-clean",
            "span_id": "span-clean",
            "tenant_id": "t",
            "aid": "a",
            "timestamp_ns": int(time.time_ns()),
            "provider": "anthropic",
        },
        "tool_name": "Bash",
        "tool_args_json": "{}",
        "role_id": "r",
    }
    res = client.post("/evaluate", json=payload)
    assert res.status_code == 200, res.text
    traces = res.json().get("step_traces", {})
    assert "aegis.coach.provider_drift" not in traces
