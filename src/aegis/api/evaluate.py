"""POST /evaluate — main entrypoint that composes the Firewall.

ATMU below = Agent Telemetry Management Unit (patent §5A).

Stages, per patent v7.10 §5 + §5A:

    build_atv                          (M8)
    ATMU.append_tentative              (M10)
    run_firewall (310..340)            (existing)
    ATMU.transition → prepared/aborted (M10)
    step 350  approval dispatch        (M9, for REQUIRE_APPROVAL)
    step 360  sign + append to audit   (M9)
    ATMU.transition → committed        (M10, after sign)
    step 370  exec-gate annotation     (M9)

PR-D: ``POST /evaluate/openclaw`` — companion route accepting the
flat schema that ``@openclaw/plugin-aegis`` posts. The plugin doesn't
know Aegis's internal ATVInput shape; this adapter receives a friendly
flat body and constructs ATVInput before calling _evaluate_impl.
"""

from __future__ import annotations

import hashlib
import json as _json
import time
import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from aegis.atmu import (
    IntentLog,
    TxState,
    make_checkpoint,
    plan_for,
)
from aegis.atv.builder import build_atv
from aegis.audit.encrypted_journal import EncryptedJournal
from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.sqlite_store import AuditDB
from aegis.burnin import BurnInController
from aegis.cost import (
    CostAttestationLedger,
    compute_divergence,
    evaluate_escalation,
)
from aegis.firewall import step350_approval, step360_audit, step370_exec
from aegis.firewall.core import run_firewall
from aegis.firewall.step320_blast import TOOL_BLAST_TABLE, UNKNOWN_TOOL_BLAST
from aegis.schema import ATVHeader, ATVInput, Verdict


class OpenClawEvaluateRequest(BaseModel):
    """Flat request shape posted by ``@openclaw/plugin-aegis``.

    Mirrors ``openclaw-plugin/src/types.ts:AegisEvaluateRequest`` so
    the plugin author writes idiomatic TypeScript without learning
    Aegis's internal 30-subfield ATVInput. The adapter on this side
    constructs the full ATVInput from the flat shape.

    The plugin's TS contract uses snake_case field names (matching
    Pydantic conventions) so deserialization is direct.
    """

    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str = "openclaw"
    session_id: str | None = None
    invocation_id: str | None = None
    provider: str | None = None
    channel: str | None = None
    user_prompt: str | None = None
    recent_turns: list[dict[str, Any]] | None = None


def _build_atv_from_openclaw(
    req: OpenClawEvaluateRequest,
) -> ATVInput:
    """Adapter: ``OpenClawEvaluateRequest`` → ``ATVInput``.

    Maps the plugin's flat fields onto the structured ATV header +
    body. Fields the plugin doesn't expose (ATS scalars, AID
    transitions, encryption metadata) are left at their schema
    defaults — the firewall steps that read them treat zeros as
    "not measured" and degrade gracefully.
    """
    aid = req.session_id or req.invocation_id or "openclaw-default"
    trace_id = req.invocation_id or uuid.uuid4().hex
    span_id = uuid.uuid4().hex
    ts_ns = time.time_ns()

    header = ATVHeader(
        trace_id=trace_id,
        span_id=span_id,
        tenant_id=req.tenant_id,
        aid=aid,
        timestamp_ns=ts_ns,
        # PR-D — first-class multi-channel + provider attribution.
        channel=req.channel,
        provider=req.provider,
        # Patent-aligned identifiers — explicit so the validator
        # doesn't have to guess.
        session_id=aid,
        agent_instance_id=aid,
    )

    return ATVInput(
        header=header,
        tool_name=req.tool_name,
        tool_args_json=_json.dumps(req.tool_input, sort_keys=True, default=str),
        # OpenClaw plugin can later carry plan_text via user_prompt,
        # though it's a coarse mapping (the prompt is *what the user
        # asked*, not *the agent's plan*).
        plan_text=(req.user_prompt or "")[:500],
    )


def _evaluate_impl(
    inp: ATVInput,
    *,
    key: Any,
    db: AuditDB,
    log: JsonlStore,
    intent_log: IntentLog | None = None,
    burnin_controller: BurnInController | None = None,
    cost_ledger: CostAttestationLedger | None = None,
    encrypted_journal: EncryptedJournal | None = None,
    burn_in_id: str | None = None,
) -> Verdict:
    # Auto-fill the Burn-in id into the ATV header so every audit record
    # carries the measurement of the software that produced it. Caller-
    # supplied burn_in_id always wins (useful for cross-attesting a
    # different deployment).
    if burn_in_id and not inp.header.burn_in_id:
        inp = inp.model_copy(
            update={"header": inp.header.model_copy(update={"burn_in_id": burn_in_id})}
        )

    # v2.3: HW telemetry simulator. AEGIS_HW_PROVIDER=sim populates the
    # 200-D HW band from a deterministic emulator (the bridge between v2.2
    # zero-fill and a real T3 hardware path). AEGIS_HW_INJECT_ATTACK can
    # rewrite specific counters so the M12 cost-divergence escalation
    # (Claim 27) fires for demo / testing.
    from aegis.hw_telemetry import simulate_from_env

    hw_counters = simulate_from_env(inp)

    # v3.2 closed loop — backfill cost band's s-10/s-11 from the rolling
    # per-(tenant, aid) EWMA when the host hasn't measured them. Host
    # values are never overwritten.
    from aegis.performance import get_default_store
    _perf = get_default_store().get(
        tenant_id=inp.header.tenant_id, aid=inp.header.aid,
    )
    if not _perf.is_empty():
        if inp.cost_estimate.cache_hit_rate == 0.0 and _perf.cache_hit_rate > 0.0:
            inp.cost_estimate.cache_hit_rate = _perf.cache_hit_rate
        if (
            inp.cost_estimate.context_utilization_ratio == 0.0
            and _perf.context_utilization_ratio > 0.0
        ):
            inp.cost_estimate.context_utilization_ratio = _perf.context_utilization_ratio

    # M8: build the 30-subfield ATV (HW band stays zero unless hw is given).
    atv = build_atv(inp, hw=hw_counters)
    atv_id = str(uuid.uuid4())
    atv_commitment = hashlib.sha3_256(atv.tobytes()).hexdigest()
    args_hash = hashlib.sha3_256(inp.tool_args_json.encode("utf-8")).hexdigest()
    blast = TOOL_BLAST_TABLE.get(inp.tool_name, UNKNOWN_TOOL_BLAST)

    # M10: ATMU — record tentative intent BEFORE firewall runs (§5A)
    intent_record_id: str | None = None
    if intent_log is not None:
        checkpoint = make_checkpoint(inp, blast)
        rec = intent_log.append_tentative(
            aid=inp.header.aid,
            tenant_id=inp.header.tenant_id,
            trace_id=inp.header.trace_id,
            span_id=inp.header.span_id,
            parent_span_id=inp.header.parent_span_id,
            tool_name=inp.tool_name,
            tool_args_hash=args_hash,
            blast_radius=blast,
            atv_commitment=atv_commitment,
            checkpoint_id=checkpoint["checkpoint_id"] if checkpoint else None,
            cost_profile=inp.header.cost_attestation_profile,
        )
        intent_record_id = rec["record_id"]
        # For tools with irreversible side effects, attach a compensation
        # plan now so it's persisted alongside the intent.
        comp = plan_for(inp.tool_name)
        if comp is not None:
            intent_log.set_compensation_plan(intent_record_id, comp)

    # Firewall 310..340 (pre-commit gate)
    verdict = run_firewall(atv, inp, atv_id=atv_id)

    # M10: ATMU first transition — prepared (firewall passed) or aborted (block).
    # APPROVAL stays in TENTATIVE → PREPARED so the human can later commit.
    if intent_log is not None and intent_record_id:
        if verdict.decision == "BLOCK":
            intent_log.transition(
                intent_record_id, new_state=TxState.ABORTED,
                reason=f"firewall block: {verdict.reason}",
            )
        else:
            intent_log.transition(
                intent_record_id, new_state=TxState.PREPARED,
                reason=f"firewall {verdict.decision.lower()}",
            )

    # Step 350: REQUIRE_APPROVAL → notification dispatch
    step350_approval.dispatch(verdict, inp)

    # Step 360: canonical serialize + Ed25519 sign + append to audit log
    record = step360_audit.sign_and_append(
        atv=atv, verdict=verdict, inp=inp, key=key, db=db, log=log
    )
    verdict.signature = record["signature"]

    # ContextMemory write — analytics-shaped projection of the audit
    # record. Separate file from audit.jsonl by design (silicon
    # roadmap: CXL SSD / Computational SSD emulation). Fully
    # defensive — must never block the verdict path.
    try:
        from aegis.context_memory import append as cm_append
        cm_append(record, mode="sidecar")
    except Exception:  # noqa: BLE001 — analytics writes never block
        pass

    # M15: also append the (already-signed) record into the encrypted
    # power-fail-safe journal. The audit log keeps plaintext for
    # operator inspection; the journal is the at-rest-encrypted source
    # of truth for forensic replay (¶[0102E]-[0102G]).
    if encrypted_journal is not None:
        encrypted_journal.append(record)

    # M12: Cost Attestation Ledger (Claims 3, 30, 34) — separate signed
    # store with its own key. Append a record for every evaluation; the
    # 'cost_attestation_hint' marker on the audit record (M9) signals
    # which audit entries were cost-influenced for downstream indexing.
    if cost_ledger is not None:
        # v2.3: when AEGIS_HW_PROVIDER=sim is on, feed the simulated HW
        # counters into compute_divergence so the j-14 / j-15 / j-16
        # metrics light up (Claim 26). Honest agents diverge <0.10 →
        # well below the 0.30 escalation threshold; attack-injection
        # rewrites a counter so the gate fires (Claim 27).
        hw_flops = hw_counters.flops_observed if hw_counters is not None else 0.0
        hw_hbm = hw_counters.hbm_bytes_observed if hw_counters is not None else 0.0
        divergence = compute_divergence(
            inp.cost_estimate,
            model_name=(inp.header.model_hash or "default"),
            hw_flops_observed=hw_flops,
            hw_hbm_bytes_observed=hw_hbm,
        )
        cost_ledger.append(
            atv_commitment=atv_commitment,
            header=inp.header,
            sw_cost_metrics=inp.cost_estimate,
            divergence=divergence,
            model_name=inp.header.model_hash,
        )
        # Claim 27: cost-divergence escalation INDEPENDENT of sLLM verdict.
        decision = evaluate_escalation(divergence)
        if decision.triggered and verdict.decision == "ALLOW":
            verdict.decision = "REQUIRE_APPROVAL"
            verdict.reason = decision.reason
            verdict.step_traces["aegis.cost.escalation"] = (
                f"step-cost: {decision.metric}={decision.observed:.3f} > "
                f"threshold {decision.threshold:.3f}"
            )

    # M10: ATMU final transition — ALLOW commits, APPROVAL stays prepared
    # (committed at /approve time), BLOCK already aborted above.
    if intent_log is not None and intent_record_id and verdict.decision == "ALLOW":
        intent_log.transition(
            intent_record_id, new_state=TxState.COMMITTED,
            reason="audit signed, host may execute",
        )

    # Step 370: exec-recommendation annotation
    step370_exec.annotate(verdict)

    # Surface the ATMU record_id in the Verdict step_traces so the host
    # can refer to it later via /tool-outcome.
    if intent_record_id:
        verdict.step_traces["aegis.atmu.intent_log"] = (
            f"intent_record_id={intent_record_id}"
        )

    # M11: feed every observation into the Burn-in controller so its
    # per-(L1..L5) sample counts and the composite anomaly score reflect
    # live traffic.
    if burnin_controller is not None:
        burnin_controller.observe(inp, verdict)
        verdict.step_traces["aegis.burnin.composite_score"] = (
            f"composite={burnin_controller.composite_score(inp):.3f}"
        )

        # Gap C (#146) — fire per-(aid × provider) divergence advisor
        # at *evaluation* time, not just at report time. The detector
        # is read-only and short-circuits cheaply when the aid hasn't
        # accumulated >=2 real-provider slots, so it's safe in the
        # firewall hot path. When divergence is found, surface it via
        # step_traces so the audit record carries the signal forward.
        try:
            drifts = burnin_controller.provider_drift_for_aid(
                inp.header.tenant_id,
                inp.role_id or "default-role",
                inp.header.aid,
            )
        except Exception:  # noqa: BLE001 — defensive; advisor must not crash eval
            drifts = []
        if drifts:
            d = drifts[0]
            verdict.step_traces["aegis.coach.provider_drift"] = (
                f"aid={d['aid']} {d['max_provider']}={d['max_rate']:.2%} "
                f"vs {d['min_provider']}={d['min_rate']:.2%} "
                f"ratio={d['ratio']:.1f}× kind={d['kind']}"
            )

    return verdict


def make_router(
    *,
    key: Any,
    db: AuditDB,
    log: JsonlStore,
    intent_log: IntentLog | None = None,
    burnin_controller: BurnInController | None = None,
    cost_ledger: CostAttestationLedger | None = None,
    encrypted_journal: EncryptedJournal | None = None,
    burn_in_id: str | None = None,
) -> APIRouter:
    r = APIRouter()

    @r.post("/evaluate", response_model=Verdict)
    def evaluate(inp: ATVInput) -> Verdict:
        return _evaluate_impl(
            inp, key=key, db=db, log=log,
            intent_log=intent_log, burnin_controller=burnin_controller,
            cost_ledger=cost_ledger, encrypted_journal=encrypted_journal,
            burn_in_id=burn_in_id,
        )

    @r.post("/evaluate/openclaw", response_model=Verdict)
    def evaluate_openclaw(req: OpenClawEvaluateRequest) -> Verdict:
        """Companion route for ``@openclaw/plugin-aegis``.

        Plugin doesn't know ATVInput's full schema; it posts the flat
        :class:`OpenClawEvaluateRequest` shape. This route adapts and
        forwards to the same firewall pipeline as ``/evaluate``.
        """
        inp = _build_atv_from_openclaw(req)
        return _evaluate_impl(
            inp, key=key, db=db, log=log,
            intent_log=intent_log, burnin_controller=burnin_controller,
            cost_ledger=cost_ledger, encrypted_journal=encrypted_journal,
            burn_in_id=burn_in_id,
        )

    return r
