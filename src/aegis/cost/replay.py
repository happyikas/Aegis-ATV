"""Cost replay harness — run a Claude Code transcript through the
firewall and observe per-call cost evolution.

This is the experimentation entry point referenced by docs/MANUAL_MACMINI.md
§"Cost monitoring": you point it at a real (or synthesized) transcript,
override knobs (budget, model, HW provider, attack injection), and it
walks every tool_use turn-by-turn, building an ATVInput at that
**partial** transcript state, running the full firewall pipeline, and
also (when HW counters are present) computing the M12 cost-divergence
escalation that sidecar mode does in :mod:`aegis.api.evaluate`.

Pure function — no I/O beyond reading the transcript file. Suitable
for unit tests, ``aegis cost replay`` CLI, and notebook prototypes.

Design notes
------------
* **Per-turn state**: tokens accumulate as we walk the transcript; at
  each tool_use line we snapshot the current cumulative_tokens /
  cumulative_dollars / input_token_count / output_token_count and
  build an ATVInput with that ``cost_estimate``.
* **Budget override**: we monkey-set ``TENANT_BUDGETS`` for a unique
  per-call tenant id so step335 sees the requested ceiling without
  affecting concurrent callers (test isolation).
* **HW provider**: ``hw_provider="sim"`` runs
  :func:`aegis.hw_telemetry.simulator.simulate` so step337 + M12
  fire on real signals; ``hw_attack`` corresponds to the simulator's
  ``ATTACK_MODES``.
* **M12 escalation**: matches sidecar's logic at evaluate.py:194 —
  if the firewall returned ALLOW but ``evaluate_escalation`` triggers,
  we override to REQUIRE_APPROVAL with the escalation reason.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegis.atv.builder import build_atv
from aegis.cost.divergence import compute_divergence
from aegis.cost.escalation import ESCALATION_MULTIPLIER, evaluate_escalation
from aegis.cost.model_flops import expected_flops
from aegis.firewall.core import run_firewall
from aegis.firewall.step335_cost import DEFAULT_BUDGET, TENANT_BUDGETS
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics

DEFAULT_DOLLAR_PER_FLOP: float = 1.5e-15


@dataclass(frozen=True)
class ReplayConfig:
    """Inputs to :func:`replay`. Defaults match the plugin's runtime."""

    transcript_path: Path
    budget_dollars: float = 1.0
    model_for_cost: str = "claude-haiku-4-5"
    hw_provider: str = "none"        # "none" | "sim"
    hw_attack: str = ""              # comma-separated subset of ATTACK_MODES
    multiplier: float = ESCALATION_MULTIPLIER  # M12 escalation multiplier


@dataclass(frozen=True)
class ReplayCall:
    """One tool_use observed in the transcript, with the firewall's
    decision at that point in time."""

    turn_idx: int
    tool_name: str
    cumulative_tokens: float
    cumulative_dollars: float
    decision: str                              # ALLOW | BLOCK | REQUIRE_APPROVAL
    reason: str
    step335_trace: str
    step337_trace: str
    cost_escalation_triggered: bool
    cost_escalation_metric: str | None
    cost_escalation_observed: float


@dataclass
class ReplaySummary:
    """Aggregate result of :func:`replay`. Mutable so callers can
    pretty-print or further analyse before serialising."""

    config: ReplayConfig
    n_turns_total: int = 0
    n_tool_calls: int = 0
    final_cumulative_tokens: float = 0.0
    final_cumulative_dollars: float = 0.0
    n_allow: int = 0
    n_block: int = 0
    n_approval: int = 0
    n_step335_escalations: int = 0
    n_m12_escalations: int = 0
    first_escalation_turn: int | None = None
    calls: list[ReplayCall] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────


def _budget_dollars_from_flops(
    in_tokens: float, out_tokens: float, model_name: str,
    dollar_per_flop: float = DEFAULT_DOLLAR_PER_FLOP,
) -> float:
    return expected_flops(model_name, in_tokens, out_tokens) * dollar_per_flop


def _make_atv_input(
    *,
    tenant_id: str,
    aid: str,
    tool_name: str,
    tool_input: dict[str, Any],
    cumulative_in_tokens: float,
    cumulative_out_tokens: float,
    cumulative_reasoning_tokens: float,
    model_for_cost: str,
) -> ATVInput:
    args_json = json.dumps(tool_input or {}, sort_keys=True, default=str)
    invocation_id = uuid.uuid4().hex
    h = hashlib.sha3_256(invocation_id.encode()).hexdigest()
    cum_dollars = _budget_dollars_from_flops(
        cumulative_in_tokens, cumulative_out_tokens, model_for_cost,
    )
    cost = CostEfficiencyMetrics(
        input_token_count=0.0,    # per-call values aren't known mid-replay
        output_token_count=0.0,
        reasoning_token_count=0.0,
        cumulative_tokens=(
            cumulative_in_tokens
            + cumulative_out_tokens
            + cumulative_reasoning_tokens
        ),
        cumulative_dollars=cum_dollars,
    )
    header = ATVHeader(
        trace_id=h[:32],
        span_id=h[32:48],
        tenant_id=tenant_id,
        aid=aid,
        timestamp_ns=time.time_ns(),
    )
    return ATVInput(
        header=header,
        plan_text="",
        tool_name=tool_name or "unknown",
        tool_args_json=args_json,
        cost_estimate=cost,
    )


def _hw_counters_for(inp: ATVInput, provider: str, attack: str):  # type: ignore[no-untyped-def]
    """Return HWCounters or None per provider — mirrors evaluate.py:68."""
    if provider != "sim":
        return None
    from aegis.hw_telemetry.simulator import simulate

    return simulate(inp, attack=attack)


# Trace key constants — keeping these in one place lets the CLI / tests
# parse them without duplicating string literals.
_S335_KEY = "aegis.firewall.step335_cost.run"
_S337_KEY = "aegis.firewall.step337_hw_anomaly.run"


def replay(config: ReplayConfig) -> ReplaySummary:
    """Walk ``config.transcript_path`` and replay every tool_use through
    the firewall. Returns one :class:`ReplayCall` per tool_use plus a
    summary block."""
    summary = ReplaySummary(config=config)
    if not config.transcript_path.is_file():
        return summary

    # Per-replay tenant id — its budget override lives only as long as
    # this function runs, then we restore TENANT_BUDGETS.
    tenant_id = f"replay-{uuid.uuid4().hex[:12]}"
    aid = f"replay-aid-{uuid.uuid4().hex[:8]}"
    TENANT_BUDGETS[tenant_id] = {
        "dollars": float(config.budget_dollars),
        **{k: v for k, v in DEFAULT_BUDGET.items() if k != "dollars"},
    }
    try:
        cum_in = cum_out = cum_reason = 0.0
        turn_idx = 0
        for line in config.transcript_path.read_text(
            encoding="utf-8"
        ).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            turn_idx += 1
            summary.n_turns_total = turn_idx

            kind = ev.get("type") or ev.get("role") or ""

            # 1) Accumulate tokens from assistant messages.
            if kind in ("assistant", "model_response", "claude"):
                usage = ev.get("usage") or {}
                cum_in     += float(usage.get("input_tokens", 0) or 0)
                cum_out    += float(usage.get("output_tokens", 0) or 0)
                cum_reason += float(usage.get("reasoning_tokens", 0) or 0)

            # 2) On every tool_use (assistant tool_use OR top-level
            #    tool_call event), build an ATV at the CURRENT cumulative
            #    state and run the firewall.
            tool_uses = _extract_tool_uses(ev, kind)
            for tu in tool_uses:
                inp = _make_atv_input(
                    tenant_id=tenant_id,
                    aid=aid,
                    tool_name=tu["name"],
                    tool_input=tu["input"],
                    cumulative_in_tokens=cum_in,
                    cumulative_out_tokens=cum_out,
                    cumulative_reasoning_tokens=cum_reason,
                    model_for_cost=config.model_for_cost,
                )
                hw = _hw_counters_for(inp, config.hw_provider, config.hw_attack)
                atv = build_atv(inp, hw=hw)
                verdict = run_firewall(atv, inp, atv_id=inp.header.span_id)

                # M12 cost-divergence escalation — mirror evaluate.py:182.
                escalation_triggered = False
                escalation_metric: str | None = None
                escalation_observed = 0.0
                if hw is not None:
                    div = compute_divergence(
                        inp.cost_estimate,
                        model_name=config.model_for_cost,
                        hw_flops_observed=hw.flops_observed,
                        hw_hbm_bytes_observed=hw.hbm_bytes_observed,
                    )
                    decision = evaluate_escalation(
                        div, multiplier=config.multiplier,
                    )
                    if decision.triggered:
                        escalation_triggered = True
                        escalation_metric = decision.metric
                        escalation_observed = decision.observed
                        # Mirror sidecar's behaviour: ALLOW + escalation
                        # → REQUIRE_APPROVAL with the escalation reason.
                        if verdict.decision == "ALLOW":
                            verdict.decision = "REQUIRE_APPROVAL"
                            verdict.reason = decision.reason

                cum_dollars = inp.cost_estimate.cumulative_dollars
                cum_tokens = inp.cost_estimate.cumulative_tokens

                call = ReplayCall(
                    turn_idx=turn_idx,
                    tool_name=tu["name"],
                    cumulative_tokens=cum_tokens,
                    cumulative_dollars=cum_dollars,
                    decision=verdict.decision,
                    reason=verdict.reason or "",
                    step335_trace=str(verdict.step_traces.get(_S335_KEY, "")),
                    step337_trace=str(verdict.step_traces.get(_S337_KEY, "")),
                    cost_escalation_triggered=escalation_triggered,
                    cost_escalation_metric=escalation_metric,
                    cost_escalation_observed=escalation_observed,
                )
                summary.calls.append(call)
                summary.n_tool_calls += 1

                if verdict.decision == "ALLOW":
                    summary.n_allow += 1
                elif verdict.decision == "BLOCK":
                    summary.n_block += 1
                else:
                    summary.n_approval += 1
                if "step335" in (verdict.reason or "").lower() or (
                    "cumulative_dollars" in (verdict.reason or "")
                    or "forecasted_cost" in (verdict.reason or "")
                ):
                    summary.n_step335_escalations += 1
                if escalation_triggered:
                    summary.n_m12_escalations += 1
                if (
                    summary.first_escalation_turn is None
                    and verdict.decision != "ALLOW"
                ):
                    summary.first_escalation_turn = turn_idx
    finally:
        TENANT_BUDGETS.pop(tenant_id, None)

    summary.final_cumulative_tokens = cum_in + cum_out + cum_reason
    summary.final_cumulative_dollars = _budget_dollars_from_flops(
        cum_in, cum_out, config.model_for_cost,
    )
    return summary


def _extract_tool_uses(
    ev: dict[str, Any], kind: str
) -> list[dict[str, Any]]:
    """Pull tool_use blocks out of a transcript line, supporting both
    flat ``tool_use`` events and embedded blocks inside ``assistant``
    content arrays. Tool name is normalised, input defaults to {}."""
    out: list[dict[str, Any]] = []

    if kind in ("tool_use", "tool_call"):
        out.append(
            {
                "name": str(ev.get("name") or ev.get("tool_name") or "unknown"),
                "input": ev.get("input") or ev.get("tool_input") or {},
            }
        )
        return out

    if kind in ("assistant", "model_response", "claude"):
        content = ev.get("content")
        if isinstance(content, list):
            for blk in content:
                if (
                    isinstance(blk, dict)
                    and blk.get("type") in ("tool_use", "tool_call")
                ):
                    out.append(
                        {
                            "name": str(
                                blk.get("name") or blk.get("tool_name") or "unknown"
                            ),
                            "input": blk.get("input") or blk.get("tool_input") or {},
                        }
                    )
    return out
