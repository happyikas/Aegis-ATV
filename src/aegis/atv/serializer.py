"""ATV → sLLM serializer (v5.0 PR-β).

The patent intent (option (b)) is that the 2080-D ATV is, by itself,
sufficient context for an sLLM to understand runtime semantics. This
module is the bridge: it walks every band of an ATV and produces a
structured, tokenisable prompt that an sLLM can read.

Two modes
---------

* **strict** — only ATV-derived data. ATVInput is NOT consulted.
  The output reflects exactly what the ATV alone carries.
  Gaps (e.g., "agent_state_embedding present but no top-k concept
  decoder available", "action_history is hash-only") are surfaced
  as explicit lines so a downstream sLLM judge can know what it
  doesn't know.

* **enriched** — strict output + supplementation from ATVInput
  (plan_text, tool_args_json, recent_actions). This is what current
  Phi-3 / Haiku would actually consume. The DELTA between strict
  and enriched is the diagnostic that drives the (b.pragmatic)
  schema-extension decision.

The diagnostic
--------------

Each call returns :class:`SerializedATV` with three fields:

* ``text`` — the prompt-ready string
* ``gaps`` — list of explicit "what's missing for full LLM context"
  observations (e.g., "plan_text_embedding ABSENT — currently in
  ATVInput.plan_text only, not in ATV proper")
* ``bands_present`` — per-band one-line summary so callers can
  inspect which bands carried signal vs which were zero-filled

The first two together answer "is ATV-2080 alone sufficient?" — see
``demo/atv_serializer_demo.py`` for a side-by-side run.

Privacy
-------

The serializer NEVER includes raw token-level attention scores or
the prompt body verbatim — those carry secret-position risk
(addressed by AttentionSummaryGuard, PR #54). Embeddings are
summarised statistically (magnitude, sparsity, top-k axis indices)
not by reverse-search. ATV bands that are *opaque by design*
(hash-expanded fingerprints) are surfaced as their fingerprint
prefix, not their float content.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from aegis.schema import (
    ATV_DIM,
    ATV_VERSION,
    SLICE_ACTION_BLAST_RADIUS,
    SLICE_ACTION_HISTORY,
    SLICE_AGENT_STATE_EMBEDDING,
    SLICE_AID_ATS_SCALARS,
    SLICE_AID_TAG_TRANSITIONS,
    SLICE_ATMU_ANOMALY,
    SLICE_COST_EFFICIENCY_METRICS,
    SLICE_DMA_FANOUT,
    SLICE_ENCRYPTION_METADATA,
    SLICE_GPU_ACCELERATOR_STATE,
    SLICE_GROUNDING_METRICS,
    SLICE_HUMAN_OVERSIGHT_STATE,
    SLICE_HW_BAND,
    SLICE_HW_COST_ATTESTATION,
    SLICE_HYPERVISOR_SIGNALS,
    SLICE_INTER_AGENT_GRAPH,
    SLICE_LINKAGE_CONSISTENCY,
    SLICE_MCP_TRUST_SIGNALS,
    SLICE_MEMORY_PROVENANCE,
    SLICE_MEMORY_TIMING_HISTOGRAMS,
    SLICE_NETWORK_TELEMETRY,
    SLICE_NOVELTY_SCORE,
    SLICE_OUTPUT_CHANNEL_DIVERSITY,
    SLICE_OUTPUT_CONTENT_FINGERPRINT,
    SLICE_PROMPT_STRUCTURE,
    SLICE_QOM_SCORES,
    SLICE_RESOURCE_ACCESS_PATTERN,
    SLICE_SESSION_BEHAVIORAL_DRIFT,
    SLICE_THERMAL_ECC_DRIFT,
    SLICE_TOOL_ARG_INSPECTION,
    SLICE_WATCHDOG_SIGNALS,
    ATVInput,
)

SerializerMode = Literal["strict", "enriched"]


# ─────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SerializedATV:
    """One serialization run.

    Attributes
    ----------
    text:
        Prompt-ready string. Header + sections separated by blank
        lines. Ends with [/ATV-CONTEXT].
    gaps:
        Explicit list of "what ATV-only mode could not represent
        well". Each entry names the band + what's missing + where
        the equivalent content lives outside ATV (e.g., in
        ATVInput.plan_text). This is the data we use to scope the
        eventual ATV schema v5 extension.
    bands_present:
        Per-band one-line status. Useful for diagnostics ("which
        bands actually carried signal in this ATV?").
    mode:
        Echo of the mode this was generated under, for audit.
    """

    text: str
    gaps: list[str] = field(default_factory=list)
    bands_present: dict[str, str] = field(default_factory=dict)
    mode: SerializerMode = "strict"

    def __len__(self) -> int:
        return len(self.text)

    def line_count(self) -> int:
        return self.text.count("\n") + 1


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _is_zero(band: np.ndarray, tol: float = 1e-9) -> bool:
    return bool(np.all(np.abs(band) < tol))


def _summarise_embedding(band: np.ndarray) -> tuple[str, list[str]]:
    """Statistical summary of a semantic embedding band.

    Returns ``(line, gaps)``. ``line`` is one human-readable line.
    ``gaps`` lists what we *can't* extract semantically without a
    decoder (top-k similar concept lookup would need a labelled
    corpus / case_memory).
    """
    if _is_zero(band):
        return ("zero-filled (no embedding source)", [])
    magnitude = float(np.linalg.norm(band))
    nonzero = int(np.sum(np.abs(band) > 1e-6))
    sparsity = 1.0 - nonzero / band.size
    top_k = int(min(5, band.size))
    # Indices of largest absolute components (compact identity proxy).
    top_idx = np.argsort(np.abs(band))[::-1][:top_k]
    top_summary = ", ".join(
        f"d{int(i)}={float(band[int(i)]):+.2f}" for i in top_idx
    )
    line = (
        f"present, |v|={magnitude:.2f}, sparsity={sparsity * 100:.1f}%, "
        f"top axes [{top_summary}]"
    )
    gaps = [
        "agent_state_embedding: 768-D vector present but no top-k "
        "concept decoder is wired in this serializer — sLLM cannot "
        "recover the underlying semantic phrase from the embedding "
        "alone. Future PR-α: add case_memory.npz nearest-neighbour "
        "lookup or a learned text-decoder head.",
    ]
    return (line, gaps)


def _summarise_hash_band(band: np.ndarray, *, label: str) -> str:
    """Hash-expanded bands (action_history, inter_agent_graph,
    memory_provenance) are SHA3-derived floats. They have NO
    semantic content — they're integrity fingerprints.

    We surface them as their digest prefix. The sLLM can use them
    to recognise repeated state ("same fingerprint as 3 turns ago")
    but cannot decode them to text.
    """
    if _is_zero(band):
        return "zero-filled (no source data)"
    digest = hashlib.sha3_256(band.tobytes()).hexdigest()[:16]
    return (
        f"{label} fingerprint: {digest}…  "
        "(hash-derived, no recoverable semantic content)"
    )


def _summarise_scalar_band(
    band: np.ndarray, *, label: str, slot_names: list[str] | None = None,
) -> str:
    """Scalar bands like cost_efficiency_metrics, blast_radius —
    interpretable per-slot floats. We surface non-zero slots only,
    in natural language.
    """
    if _is_zero(band):
        return f"{label}: all-zero (no signal)"
    parts: list[str] = []
    for i, v in enumerate(band):
        if abs(float(v)) < 1e-6:
            continue
        name = (
            slot_names[i] if slot_names and i < len(slot_names)
            else f"slot{i}"
        )
        parts.append(f"{name}={float(v):.3f}")
    if not parts:
        return f"{label}: ~zero"
    return f"{label}: " + ", ".join(parts)


def _format_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# Slot names per band (best-effort, lifted from schema docstrings).
_COST_EFFICIENCY_SLOTS = [
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cumulative_tokens",
    "cumulative_dollars",
    "tokens_per_success",
    "tokens_per_step",
    "tokens_per_output_byte",
    "reasoning_to_action_ratio",
    "cache_hit_rate",
    "context_util_ratio",
    "cost_delta_vs_role",
    "budget_burn_rate",
    "forecasted_cost",
    "task_progress",
    "marginal_value",
]

_PROMPT_STRUCTURE_SLOTS = [
    "length_norm",
    "line_density",
    "uppercase_ratio",
    "punctuation_density",
    "has_injection_kw",
    "has_system_kw",
    "has_code_block",
    "url_count_norm",
    "all_caps_tokens",
    # v4.3 attention-summary fold-in slots
    "attn_entropy_norm",
    "attn_top_k_concentration",
    "attn_sink_presence",
    "attn_recency_bias",
    "attn_effective_rank",
    "reserved_14",
    "reserved_15",
]

_NOVELTY_SLOTS = [
    "embedding_distance",
    "tool_novelty",
    "structural_novelty",
    "composite",
]

_BLAST_RADIUS_SLOTS = [
    "filesystem_writes",
    "network_egress",
    "process_spawns",
    "external_api_calls",
    "shared_resource_writes",
    "destructive_flag",
    "irreversible_flag",
    "broadcast_flag",
    "secret_access",
    "privileged_op",
    "cross_tenant",
    "log_volume",
    "reserved_12",
    "reserved_13",
    "reserved_14",
    "reserved_15",
]


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def atv_to_prompt(
    atv: np.ndarray,
    inp: ATVInput | None = None,
    *,
    mode: SerializerMode = "strict",
) -> SerializedATV:
    """Serialise a 2080-D ATV into an sLLM-ready prompt.

    Parameters
    ----------
    atv:
        The full 2080-D float32 vector built by
        :func:`aegis.atv.builder.build_atv`.
    inp:
        Optional :class:`ATVInput`. ONLY consulted when ``mode ==
        "enriched"``. In ``strict`` mode it is ignored — the output
        reflects exactly what the ATV alone carries.
    mode:
        ``"strict"`` (default) for the (b.pragmatic) diagnostic;
        ``"enriched"`` for production sLLM consumption with raw-text
        fallback for the missing-semantic bands.
    """
    if atv.shape != (ATV_DIM,):
        raise ValueError(
            f"expected ATV shape ({ATV_DIM},), got {atv.shape}"
        )
    if mode == "enriched" and inp is None:
        raise ValueError("enriched mode requires ATVInput")

    lines: list[str] = []
    gaps: list[str] = []
    bands: dict[str, str] = {}

    lines.append(f"[ATV-CONTEXT mode={mode} schema={ATV_VERSION}]")

    # ── Header ──
    if inp is not None and mode == "enriched":
        lines.append("SESSION")
        lines.append(f"  tenant: {inp.header.tenant_id}")
        lines.append(f"  aid:    {inp.header.aid}")
        lines.append(f"  tier:   {inp.header.tier_profile}")
        lines.append(
            f"  attestation_profile: {inp.header.cost_attestation_profile}"
        )
        lines.append("")
    else:
        # Strict: derive header info from aid_ats_scalars hash slots.
        ats = atv[SLICE_AID_ATS_SCALARS]
        if not _is_zero(ats):
            lines.append("SESSION (from aid_ats_scalars 8-D hashes)")
            lines.append(
                f"  tenant_hash:   {float(ats[2]):.3f}  "
                f"aid_hash: {float(ats[0]):.3f}"
            )
            lines.append(
                f"  T2_flag={float(ats[3]):.0f}  "
                f"T3_flag={float(ats[4]):.0f}  "
                f"hw_attest={float(ats[5]):.0f}  "
                f"parent_span={float(ats[6]):.0f}"
            )
            lines.append("")
            gaps.append(
                "tenant_id / aid: only available as 16-bit hashes in "
                "aid_ats_scalars — original strings live in ATVHeader, "
                "outside the ATV vector. sLLM cannot reverse-lookup."
            )

    # ── SEMANTIC ──
    lines.append("SEMANTIC")

    agent_emb = atv[SLICE_AGENT_STATE_EMBEDDING]
    line, sub_gaps = _summarise_embedding(agent_emb)
    lines.append(f"  agent_state_embedding: {line}")
    bands["agent_state_embedding"] = line
    gaps.extend(sub_gaps)

    nov = atv[SLICE_NOVELTY_SCORE]
    novelty_line = _summarise_scalar_band(
        nov, label="novelty_score", slot_names=_NOVELTY_SLOTS,
    )
    lines.append(f"  {novelty_line}")
    bands["novelty_score"] = novelty_line

    # plan_text embedding — DOES NOT EXIST in ATV-2080. This is the
    # central gap for option (b.pragmatic).
    if mode == "enriched" and inp is not None and inp.plan_text:
        lines.append(
            f"  plan_text (raw, enriched supplement, "
            f"{len(inp.plan_text)} chars):"
        )
        # 400-char excerpt — enough for sLLM to ground, short enough
        # to keep prompt budget under control.
        excerpt = inp.plan_text[:400]
        if len(inp.plan_text) > 400:
            excerpt += "…"
        lines.append(f"    {excerpt!r}")
    else:
        lines.append(
            "  plan_text: ABSENT in ATV vector "
            "(text lives in ATVInput.plan_text only)"
        )
    gaps.append(
        "plan_text_embedding: NOT in ATV. The current tool-call's "
        "plan/intent text is in ATVInput.plan_text only. sLLM in "
        "strict mode cannot read what the agent is trying to do."
    )

    lines.append("")

    # ── PROMPT STRUCTURE (handcrafted features + attention summary) ──
    lines.append("PROMPT STRUCTURE")
    ps = atv[SLICE_PROMPT_STRUCTURE]
    ps_line = _summarise_scalar_band(
        ps, label="prompt_structure",
        slot_names=_PROMPT_STRUCTURE_SLOTS,
    )
    lines.append(f"  {ps_line}")
    bands["prompt_structure"] = ps_line
    lines.append("")

    # ── COST + EFFICIENCY ──
    lines.append("COST + EFFICIENCY")
    cost = atv[SLICE_COST_EFFICIENCY_METRICS]
    if _is_zero(cost):
        lines.append("  all slots zero (no cost signal yet)")
        bands["cost_efficiency_metrics"] = "zero"
    else:
        # Render as natural-language bullets for the slots we know.
        if abs(float(cost[3])) > 0:
            lines.append(f"  cumulative_tokens:   {float(cost[3]):,.0f}")
        if abs(float(cost[4])) > 0:
            lines.append(f"  cumulative_dollars:  ${float(cost[4]):.4f}")
        if abs(float(cost[9])) > 0:
            lines.append(
                f"  cache_hit_rate:      {_format_pct(float(cost[9]))}"
            )
        if abs(float(cost[10])) > 0:
            lines.append(
                f"  context_utilization: {_format_pct(float(cost[10]))}"
            )
        if abs(float(cost[14])) > 0:
            lines.append(f"  task_progress:       {float(cost[14]):.2f}")
        if abs(float(cost[12])) > 0:
            lines.append(
                f"  budget_burn_rate:    ${float(cost[12]):.4f}/min"
            )
        bands["cost_efficiency_metrics"] = (
            f"non-zero ({sum(1 for v in cost if abs(float(v)) > 0)} of 16 slots)"
        )
    lines.append("")

    # ── ACTION HISTORY (hash-only — a key gap for option b) ──
    lines.append("ACTION HISTORY")
    action_h = atv[SLICE_ACTION_HISTORY]
    line = _summarise_hash_band(action_h, label="action_history")
    lines.append(f"  {line}")
    bands["action_history"] = line
    if not _is_zero(action_h):
        gaps.append(
            "action_history (640-D): hash-expanded fingerprint, NOT "
            "semantic. sLLM can detect 'same as 3 turns ago' but "
            "cannot read what the action was. The recent_actions "
            "list lives in ATVInput.recent_actions, outside ATV."
        )
    if mode == "enriched" and inp is not None and inp.recent_actions:
        recent_lines: list[str] = []
        for act in inp.recent_actions[-5:]:
            tool = act.get("tool", "?")
            result = act.get("result", "?")
            recent_lines.append(f"    turn -{len(recent_lines) + 1}: "
                                f"{tool} → {result}")
        if recent_lines:
            lines.append("  recent_actions (enriched):")
            for ln in recent_lines:
                lines.append(ln)
    lines.append("")

    # ── INTER-AGENT GRAPH + MEMORY PROVENANCE (hash bands) ──
    lines.append("RELATIONS + PROVENANCE")
    iag = atv[SLICE_INTER_AGENT_GRAPH]
    mp = atv[SLICE_MEMORY_PROVENANCE]
    iag_line = _summarise_hash_band(iag, label="inter_agent_graph")
    mp_line = _summarise_hash_band(mp, label="memory_provenance")
    lines.append(f"  {iag_line}")
    lines.append(f"  {mp_line}")
    bands["inter_agent_graph"] = iag_line
    bands["memory_provenance"] = mp_line
    lines.append("")

    # ── BLAST RADIUS + RESOURCE ACCESS ──
    lines.append("BLAST + RESOURCES")
    blast = atv[SLICE_ACTION_BLAST_RADIUS]
    blast_line = _summarise_scalar_band(
        blast, label="blast_radius", slot_names=_BLAST_RADIUS_SLOTS,
    )
    lines.append(f"  {blast_line}")
    bands["action_blast_radius"] = blast_line

    rap = atv[SLICE_RESOURCE_ACCESS_PATTERN]
    rap_line = _summarise_scalar_band(rap, label="resource_access")
    lines.append(f"  {rap_line}")
    bands["resource_access_pattern"] = rap_line
    lines.append("")

    # ── GROUNDING + TRUST ──
    lines.append("GROUNDING + TRUST")
    grd = atv[SLICE_GROUNDING_METRICS]
    mcp = atv[SLICE_MCP_TRUST_SIGNALS]
    hum = atv[SLICE_HUMAN_OVERSIGHT_STATE]
    grd_line = _summarise_scalar_band(grd, label="grounding_metrics")
    mcp_line = _summarise_scalar_band(mcp, label="mcp_trust_signals")
    hum_line = _summarise_scalar_band(hum, label="human_oversight")
    lines.append(f"  {grd_line}")
    lines.append(f"  {mcp_line}")
    lines.append(f"  {hum_line}")
    bands["grounding_metrics"] = grd_line
    bands["mcp_trust_signals"] = mcp_line
    bands["human_oversight_state"] = hum_line
    lines.append("")

    # ── BEHAVIORAL DRIFT + QOM ──
    drift = atv[SLICE_SESSION_BEHAVIORAL_DRIFT]
    qom = atv[SLICE_QOM_SCORES]
    drift_line = _summarise_scalar_band(drift, label="behavioral_drift")
    qom_line = _summarise_scalar_band(qom, label="qom_scores")
    lines.append("BEHAVIOUR + MEMORY-QUALITY")
    lines.append(f"  {drift_line}")
    lines.append(f"  {qom_line}")
    bands["session_behavioral_drift"] = drift_line
    bands["qom_scores"] = qom_line
    lines.append("")

    # ── OUTPUT FINGERPRINT + TOOL-ARG INSPECTION ──
    out_fp = atv[SLICE_OUTPUT_CONTENT_FINGERPRINT]
    tool_arg = atv[SLICE_TOOL_ARG_INSPECTION]
    out_line = _summarise_hash_band(
        out_fp, label="output_content_fingerprint",
    )
    tool_line = _summarise_scalar_band(
        tool_arg, label="tool_arg_inspection",
    )
    lines.append("CALL ENVELOPE")
    lines.append(f"  {out_line}")
    lines.append(f"  {tool_line}")
    bands["output_content_fingerprint"] = out_line
    bands["tool_arg_inspection"] = tool_line
    lines.append("")

    # ── CHANNEL DIVERSITY + ENCRYPTION ──
    chan = atv[SLICE_OUTPUT_CHANNEL_DIVERSITY]
    enc = atv[SLICE_ENCRYPTION_METADATA]
    chan_line = _summarise_scalar_band(chan, label="output_channels")
    enc_line = _summarise_scalar_band(enc, label="encryption_metadata")
    lines.append("CHANNELS")
    lines.append(f"  {chan_line}")
    lines.append(f"  {enc_line}")
    bands["output_channel_diversity"] = chan_line
    bands["encryption_metadata"] = enc_line
    lines.append("")

    # ── HARDWARE BAND (T2 typically zero, T3 has signal) ──
    hw_full = atv[SLICE_HW_BAND]
    if _is_zero(hw_full):
        lines.append("HARDWARE")
        lines.append(
            "  all-zero (T2 software-only profile, no HW telemetry "
            "source attached)"
        )
        bands["hw_band"] = "zero (T2)"
        gaps.append(
            "hw_band (200-D): zero in T2 mode. ~10% of ATV is "
            "currently semantically empty. Reclaiming for additional "
            "semantic content under T2 build is an option for the "
            "PR-α schema decision."
        )
    else:
        # Render anomaly-only summary.
        lines.append("HARDWARE (T3 telemetry present)")
        for label, sl in [
            ("memory_timing", SLICE_MEMORY_TIMING_HISTOGRAMS),
            ("aid_tag_transitions", SLICE_AID_TAG_TRANSITIONS),
            ("atmu_anomaly", SLICE_ATMU_ANOMALY),
            ("dma_fanout", SLICE_DMA_FANOUT),
            ("thermal_ecc", SLICE_THERMAL_ECC_DRIFT),
            ("watchdog", SLICE_WATCHDOG_SIGNALS),
            ("network_telemetry", SLICE_NETWORK_TELEMETRY),
            ("gpu_state", SLICE_GPU_ACCELERATOR_STATE),
            ("hypervisor", SLICE_HYPERVISOR_SIGNALS),
            ("hw_cost_attestation", SLICE_HW_COST_ATTESTATION),
            ("linkage_consistency", SLICE_LINKAGE_CONSISTENCY),
        ]:
            sub = atv[sl]
            if _is_zero(sub):
                continue
            lines.append(
                f"  {label}: |v|={float(np.linalg.norm(sub)):.2f} "
                f"({sub.size}-D)"
            )
        bands["hw_band"] = "non-zero (T3)"
    lines.append("")

    # ── CURRENT TOOL CALL — only available via ATVInput ──
    if mode == "enriched" and inp is not None:
        lines.append("CURRENT TOOL CALL (enriched, from ATVInput)")
        lines.append(f"  tool: {inp.tool_name}")
        # Don't dump full args_json — could carry secrets. 200-char cap.
        args_excerpt = inp.tool_args_json[:200]
        if len(inp.tool_args_json) > 200:
            args_excerpt += "…"
        lines.append(f"  args: {args_excerpt}")
        lines.append("")
    else:
        lines.append("CURRENT TOOL CALL")
        lines.append(
            "  not in ATV vector "
            "(tool_name + tool_args_json live in ATVInput only)"
        )
        gaps.append(
            "current tool name + args: NOT in ATV proper. Decided "
            "elsewhere (step310/311 firewall) and surfaced via "
            "ATVInput.tool_name + .tool_args_json. sLLM in strict "
            "mode does not see what the agent is about to do."
        )
        lines.append("")

    lines.append("[/ATV-CONTEXT]")

    # De-duplicate gaps preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for g in gaps:
        if g not in seen:
            seen.add(g)
            deduped.append(g)

    return SerializedATV(
        text="\n".join(lines),
        gaps=deduped,
        bands_present=bands,
        mode=mode,
    )


# ─────────────────────────────────────────────────────────────────────
# Diagnostic helper — strict vs enriched delta
# ─────────────────────────────────────────────────────────────────────


def diagnose(
    atv: np.ndarray, inp: ATVInput,
) -> dict[str, object]:
    """Run BOTH modes and return the diff in a form the demo prints.

    The diff is the empirical answer to "is ATV-2080 alone enough for
    sLLM consumption?" — it lists exactly what the enriched mode
    surfaces that strict mode could not.
    """
    strict = atv_to_prompt(atv, mode="strict")
    enriched = atv_to_prompt(atv, inp, mode="enriched")
    # Bytes added by enrichment — proxy for "how much extra context
    # is needed for the sLLM to do its job".
    delta_bytes = len(enriched.text) - len(strict.text)
    return {
        "strict_text": strict.text,
        "enriched_text": enriched.text,
        "strict_lines": strict.line_count(),
        "enriched_lines": enriched.line_count(),
        "delta_bytes": delta_bytes,
        "strict_gaps": strict.gaps,
        "enriched_gaps": enriched.gaps,
        "strict_bytes": len(strict.text),
        "enriched_bytes": len(enriched.text),
    }
