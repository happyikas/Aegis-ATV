"""Build a 2080-D ATV-2080-v1 tensor from an ``ATVInput`` per patent
v7.10 Appendix A.

For T2 (software-only), encoders fall into four families:

  TEXT-EMBED   — pass canonical text through the embedding provider:
                 agent_state_embedding, action_history,
                 output_content_fingerprint
  HASH-EXPAND  — deterministic SHA3 expansion of structured but
                 non-textual inputs (graphs, fingerprints):
                 inter_agent_graph, memory_provenance, prompt_structure
  FEATURE-EXTRACT — compute actual scalar features from inputs:
                 qom_scores, resource_access_pattern, aid_ats_scalars,
                 encryption_metadata, tool_arg_inspection,
                 action_blast_radius, output_channel_diversity,
                 session_behavioral_drift, mcp_trust_signals,
                 grounding_metrics, novelty_score, human_oversight_state,
                 cost_efficiency_metrics
  ZERO         — T2 has no hardware telemetry; the entire HW band is
                 zero-filled per ¶[0042]. The cost_efficiency_metrics
                 carries the SW-side cost; HW cost arrives in T3.
"""

from __future__ import annotations

import hashlib
import json
import re

import numpy as np

from aegis.atv.embeddings import get_provider
from aegis.firewall.step320_blast import TOOL_BLAST_TABLE, UNKNOWN_TOOL_BLAST
from aegis.schema import (
    ALL_SUBFIELDS,
    ATV_DIM,
    SLICE_ACTION_BLAST_RADIUS,
    SLICE_ACTION_HISTORY,
    SLICE_AGENT_STATE_EMBEDDING,
    SLICE_AID_ATS_SCALARS,
    SLICE_COST_EFFICIENCY_METRICS,
    SLICE_ENCRYPTION_METADATA,
    SLICE_GROUNDING_METRICS,
    SLICE_HUMAN_OVERSIGHT_STATE,
    SLICE_HW_BAND,
    SLICE_INTER_AGENT_GRAPH,
    SLICE_MCP_TRUST_SIGNALS,
    SLICE_MEMORY_PROVENANCE,
    SLICE_NOVELTY_SCORE,
    SLICE_OUTPUT_CHANNEL_DIVERSITY,
    SLICE_OUTPUT_CONTENT_FINGERPRINT,
    SLICE_PROMPT_STRUCTURE,
    SLICE_QOM_SCORES,
    SLICE_RESOURCE_ACCESS_PATTERN,
    SLICE_SESSION_BEHAVIORAL_DRIFT,
    SLICE_TOOL_ARG_INSPECTION,
    ATVInput,
)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def _hash_to_floats(seed: bytes, dim: int) -> np.ndarray:
    """Deterministic SHA3-512 expansion → ``dim`` float32 in [-1, 1]."""
    out = np.zeros(dim, dtype=np.float32)
    i = 0
    counter = 0
    while i < dim:
        digest = hashlib.sha3_512(seed + counter.to_bytes(4, "big")).digest()
        for b in digest:
            if i >= dim:
                break
            out[i] = (b - 127.5) / 127.5
            i += 1
        counter += 1
    norm = float(np.linalg.norm(out))
    if norm > 0:
        out /= norm
    return out


def _fixed_slot_vector(named: dict[str, float], keys: list[str], dim: int) -> np.ndarray:
    """Place known keys into fixed slots; unknown keys ignored; padded to dim."""
    arr = np.zeros(dim, dtype=np.float32)
    for i, k in enumerate(keys[:dim]):
        arr[i] = float(named.get(k, 0.0))
    return arr


# ─────────────────────────────────────────────────────────────────────
# Subfield key registries (fixed slot order — patent leaves room for
# implementation, but stable order is required for cross-machine
# determinism so the audit signature matches.)
# ─────────────────────────────────────────────────────────────────────

# Behavioral & safety subfields. Slots beyond the listed keys remain 0.
SAFETY_OUTPUT_KEYS: list[str] = [
    "token_entropy", "system_prompt_overlap", "refusal_rate",
    "persona_delta", "toxicity", "prompt_injection",
    "pii_exposure", "sensitive_pattern", "hallucination_score",
    "language_shift", "code_block_density", "url_density",
    # remaining of 64 padded to 0
]
TOOL_ARG_KEYS: list[str] = [
    "destructive_verb", "path_traversal", "encoded_payload",
    "sql_keyword", "shell_special", "url_count",
    "filesystem_write", "binary_blob", "credential_pattern",
    "regex_complexity", "unicode_invisible", "base64_blob",
    # rest padded
]
BLAST_KEYS: list[str] = [
    "blast_radius_norm", "reversibility", "scope", "criticality",
    "estimated_detection_time", "reversal_cost", "recovery_difficulty",
    "external_side_effects", "data_loss_risk", "regulatory_risk",
    # rest padded to 16
]
CHANNEL_DIVERSITY_KEYS: list[str] = [
    "external_url_count", "image_ref_count", "encoded_payload_count",
    "outbound_email_count", "webhook_count", "stdout_byte_count",
    # rest padded to 12
]
SESSION_DRIFT_KEYS: list[str] = [
    "persona_drift", "refusal_rate_change", "tone_shift",
    "system_prompt_adherence_trend", "topic_drift", "verbosity_drift",
    # rest padded to 16
]
MCP_TRUST_KEYS: list[str] = [
    "server_identity_score", "tool_description_change_rate",
    "schema_diff_score", "tool_count_change", "trust_band",
    # rest padded to 12
]
GROUNDING_KEYS: list[str] = [
    "source_attribution_coverage", "citation_grounding_score",
    "confidence_calibration_error", "fact_consistency",
    # rest padded to 16
]
NOVELTY_KEYS: list[str] = [
    "mahalanobis_distance", "autoencoder_recon_error",
    "kl_divergence", "composite_novelty",
]  # exactly 4
OVERSIGHT_KEYS: list[str] = [
    "operator_presence", "approval_latency_ms", "recent_overrides",
    "human_absence_safety_timer", "approver_count", "shift_active",
    "escalation_rate", "human_response_p95",
]  # exactly 8
QOM_KEYS: list[str] = [
    "fidelity", "completeness", "freshness", "relevance",
    # 12 trend features padded to 16
]
RESOURCE_KEYS: list[str] = [
    "fs_read_count", "fs_write_count", "net_egress_bytes",
    "net_ingress_bytes", "db_read_count", "db_write_count",
    "shell_invocations", "api_calls",
    # padded to 32
]
ENC_META_KEYS: list[str] = [
    "encryption_at_rest", "encryption_in_transit", "key_age_days",
    "key_rotation_pending", "tenant_isolation_level",
    # padded to 12
]


# ─────────────────────────────────────────────────────────────────────
# Per-subfield encoders
# ─────────────────────────────────────────────────────────────────────
def encode_agent_state_embedding(inp: ATVInput) -> np.ndarray:
    canonical = json.dumps(
        {
            "aid": inp.header.aid,
            "tenant": inp.header.tenant_id,
            "role": inp.role_id or "",
            "capabilities": sorted(inp.capability_manifest),
            "state": inp.agent_state_text,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return get_provider().embed(canonical, 768)


def encode_action_history(inp: ATVInput) -> np.ndarray:
    canonical = json.dumps(inp.recent_actions[-20:], sort_keys=True, separators=(",", ":"))
    return get_provider().embed(canonical, 640)


def encode_inter_agent_graph(inp: ATVInput) -> np.ndarray:
    if not inp.inter_agent_edges:
        return np.zeros(128, dtype=np.float32)
    blob = "|".join(f"{a}->{b}" for a, b in sorted(inp.inter_agent_edges)).encode()
    return _hash_to_floats(blob, 128)


def encode_memory_provenance(inp: ATVInput) -> np.ndarray:
    if not inp.memory_fingerprint:
        return np.zeros(64, dtype=np.float32)
    return _hash_to_floats(inp.memory_fingerprint.encode(), 64)


def encode_qom_scores(inp: ATVInput) -> np.ndarray:
    return _fixed_slot_vector(inp.qom, QOM_KEYS, 16)


def encode_resource_access_pattern(inp: ATVInput) -> np.ndarray:
    return _fixed_slot_vector({}, RESOURCE_KEYS, 32)  # populated by future ATMU integration


def encode_prompt_structure(inp: ATVInput) -> np.ndarray:
    """16-D structured features of the prompt text."""
    txt = inp.plan_text
    if not txt:
        return np.zeros(16, dtype=np.float32)
    arr = np.zeros(16, dtype=np.float32)
    arr[0] = min(1.0, len(txt) / 4000.0)                                              # length-norm
    arr[1] = min(1.0, txt.count("\n") / 50.0)                                          # line density
    arr[2] = min(1.0, sum(c.isupper() for c in txt) / max(len(txt), 1))                # uppercase ratio
    arr[3] = min(1.0, sum(c in "!?" for c in txt) / max(len(txt), 1) * 50)             # punctuation density
    arr[4] = float(any(kw in txt.lower() for kw in ("ignore", "disregard", "override")))
    arr[5] = float(any(kw in txt.lower() for kw in ("system", "prompt", "instruction")))
    arr[6] = float("```" in txt)                                                       # code-block presence
    arr[7] = min(1.0, txt.count("http") / 10.0)                                        # URL count proxy
    arr[8] = min(1.0, len(re.findall(r"\b[A-Z]{4,}\b", txt)) / 10.0)                   # ALL-CAPS tokens
    return arr


def encode_aid_ats_scalars(inp: ATVInput) -> np.ndarray:
    """8-D scalar projection of (aid, ats, timestamp) — fixed-slot."""
    arr = np.zeros(8, dtype=np.float32)
    aid_h = int.from_bytes(hashlib.sha3_256(inp.header.aid.encode()).digest()[:4], "big")
    arr[0] = (aid_h % 65535) / 65535.0
    arr[1] = (inp.header.timestamp_ns % 1_000_000_000) / 1_000_000_000.0
    arr[2] = float(hash(inp.header.tenant_id) % 1000) / 1000.0
    arr[3] = 1.0 if inp.header.tier_profile == "T2" else 0.0
    arr[4] = 1.0 if inp.header.tier_profile == "T3" else 0.0
    arr[5] = 1.0 if inp.header.cost_attestation_profile in ("hardware", "both") else 0.0
    arr[6] = 1.0 if inp.header.parent_span_id else 0.0
    arr[7] = float(len(inp.header.aid)) / 64.0
    return arr


def encode_encryption_metadata(inp: ATVInput) -> np.ndarray:
    return _fixed_slot_vector(inp.encryption_meta, ENC_META_KEYS, 12)


def encode_output_content_fingerprint(inp: ATVInput) -> np.ndarray:
    """64-D: 12 named slots from safety_flags / output text features +
    52 hash-derived dimensions for cross-call diff signal."""
    arr = np.zeros(64, dtype=np.float32)
    arr[: len(SAFETY_OUTPUT_KEYS)] = _fixed_slot_vector(inp.safety_flags, SAFETY_OUTPUT_KEYS, 12)
    if inp.output_text:
        # remaining 52 dims hashed from output text
        tail = _hash_to_floats(inp.output_text.encode(), 52)
        arr[12:] = tail
    return arr


def encode_tool_arg_inspection(inp: ATVInput) -> np.ndarray:
    """32-D: structured features extracted from tool_args_json text."""
    arr = np.zeros(32, dtype=np.float32)
    s = inp.tool_args_json or ""
    if not s:
        return arr
    lower = s.lower()
    # 12 fixed-slot named features
    feats = {
        "destructive_verb":   float(any(kw in lower for kw in ("drop ", "delete ", "rm ", "truncate", "destroy"))),
        "path_traversal":     float("../" in s or "/etc/" in lower),
        "encoded_payload":    float(any(kw in s for kw in ("base64", "%2e%2e"))),
        "sql_keyword":        float(any(kw in lower for kw in ("select ", "union ", "drop table", "insert "))),
        "shell_special":      float(any(c in s for c in ("|", "&", ";", "`", "$("))),
        "url_count":          min(1.0, s.count("http") / 5.0),
        "filesystem_write":   float(any(kw in lower for kw in ("write", "create", "append"))),
        "binary_blob":        float(any(kw in s for kw in ("=base64,", ".bin", ".tar.gz"))),
        "credential_pattern": float(any(kw in s for kw in ("sk-", "AKIA", "BEGIN PRIVATE"))),
        "regex_complexity":   min(1.0, len(re.findall(r"[\\\.\*\+\?\(\)\[\]\{\}\|\^\$]", s)) / 50.0),
        "unicode_invisible":  float(any(ord(c) in (0x200B, 0x200C, 0x200D, 0xFEFF) for c in s)),
        "base64_blob":        float(bool(re.search(r"[A-Za-z0-9+/]{40,}={0,2}", s))),
    }
    arr[: len(TOOL_ARG_KEYS)] = _fixed_slot_vector(feats, TOOL_ARG_KEYS, 12)
    # 20 dims hashed from args content for stable cross-call diff
    arr[12:] = _hash_to_floats(s.encode("utf-8")[:1000], 20)
    return arr


def encode_action_blast_radius(inp: ATVInput) -> np.ndarray:
    """16-D: blast radius numeric + named slots."""
    arr = np.zeros(16, dtype=np.float32)
    blast = TOOL_BLAST_TABLE.get(inp.tool_name, UNKNOWN_TOOL_BLAST)
    arr[0] = blast / 10.0  # blast_radius_norm
    # crude reversibility/scope estimates from tool name
    irreversible_tools = {"transfer_funds", "send_email", "delete_file", "execute_shell"}
    high_scope_tools = {"db_mutation", "execute_shell", "transfer_funds"}
    arr[1] = 0.0 if inp.tool_name in irreversible_tools else 0.7  # reversibility
    arr[2] = 1.0 if inp.tool_name in high_scope_tools else 0.3    # scope
    arr[3] = blast / 10.0  # criticality (proxy)
    arr[4] = 0.5  # estimated_detection_time placeholder
    arr[5] = blast / 10.0  # reversal_cost (proxy)
    return arr


def encode_output_channel_diversity(inp: ATVInput) -> np.ndarray:
    feats: dict[str, float] = {}
    if inp.tool_args_json:
        feats["external_url_count"] = min(1.0, inp.tool_args_json.count("http") / 5.0)
        feats["image_ref_count"]    = min(1.0, sum(inp.tool_args_json.count(e) for e in (".png", ".jpg")) / 5.0)
        feats["encoded_payload_count"] = min(1.0, inp.tool_args_json.count("base64") / 3.0)
    return _fixed_slot_vector(feats, CHANNEL_DIVERSITY_KEYS, 12)


def encode_session_behavioral_drift(inp: ATVInput) -> np.ndarray:
    return _fixed_slot_vector(inp.session_behavior, SESSION_DRIFT_KEYS, 16)


def encode_mcp_trust_signals(inp: ATVInput) -> np.ndarray:
    return _fixed_slot_vector(inp.mcp_context, MCP_TRUST_KEYS, 12)


def encode_grounding_metrics(inp: ATVInput) -> np.ndarray:
    return _fixed_slot_vector(inp.grounding, GROUNDING_KEYS, 16)


def encode_novelty_score(inp: ATVInput) -> np.ndarray:
    return _fixed_slot_vector(inp.novelty, NOVELTY_KEYS, 4)


def encode_human_oversight_state(inp: ATVInput) -> np.ndarray:
    return _fixed_slot_vector(inp.oversight, OVERSIGHT_KEYS, 8)


def encode_cost_efficiency_metrics(inp: ATVInput) -> np.ndarray:
    return inp.cost_estimate.to_array()


# ─────────────────────────────────────────────────────────────────────
# Top-level builder
# ─────────────────────────────────────────────────────────────────────
def build_atv(
    inp: ATVInput,
    *,
    hw: object | None = None,
) -> np.ndarray:
    """Assemble the 2080-D ATV-2080-v1 tensor from ``inp``.

    ``hw`` is an optional :class:`aegis.hw_telemetry.HWCounters`
    instance (typed as ``object`` here to avoid a circular import).
    When provided, the 200-D HW band is populated by
    :func:`aegis.atv.hw_encoders.fill_hw_band` instead of being zero-
    filled. Caller is responsible for sourcing the counters — under
    sidecar mode, :func:`aegis.api.evaluate._evaluate_impl` calls
    :func:`aegis.hw_telemetry.simulate_from_env`.
    """
    atv = np.zeros(ATV_DIM, dtype=np.float32)

    # SW band
    atv[SLICE_AGENT_STATE_EMBEDDING]      = encode_agent_state_embedding(inp)
    atv[SLICE_ACTION_HISTORY]             = encode_action_history(inp)
    atv[SLICE_INTER_AGENT_GRAPH]          = encode_inter_agent_graph(inp)
    atv[SLICE_MEMORY_PROVENANCE]          = encode_memory_provenance(inp)
    atv[SLICE_QOM_SCORES]                 = encode_qom_scores(inp)
    atv[SLICE_RESOURCE_ACCESS_PATTERN]    = encode_resource_access_pattern(inp)
    atv[SLICE_PROMPT_STRUCTURE]           = encode_prompt_structure(inp)
    atv[SLICE_AID_ATS_SCALARS]            = encode_aid_ats_scalars(inp)
    atv[SLICE_ENCRYPTION_METADATA]        = encode_encryption_metadata(inp)
    atv[SLICE_OUTPUT_CONTENT_FINGERPRINT] = encode_output_content_fingerprint(inp)
    atv[SLICE_TOOL_ARG_INSPECTION]        = encode_tool_arg_inspection(inp)
    atv[SLICE_ACTION_BLAST_RADIUS]        = encode_action_blast_radius(inp)
    atv[SLICE_OUTPUT_CHANNEL_DIVERSITY]   = encode_output_channel_diversity(inp)
    atv[SLICE_SESSION_BEHAVIORAL_DRIFT]   = encode_session_behavioral_drift(inp)
    atv[SLICE_MCP_TRUST_SIGNALS]          = encode_mcp_trust_signals(inp)
    atv[SLICE_GROUNDING_METRICS]          = encode_grounding_metrics(inp)
    atv[SLICE_NOVELTY_SCORE]              = encode_novelty_score(inp)
    atv[SLICE_HUMAN_OVERSIGHT_STATE]      = encode_human_oversight_state(inp)
    atv[SLICE_COST_EFFICIENCY_METRICS]    = encode_cost_efficiency_metrics(inp)

    # HW band — T2 default is zero-fill per ¶[0042]. v2.3 emulator path:
    # caller passes a pre-built HWCounters from aegis.hw_telemetry.simulate.
    atv[SLICE_HW_BAND] = 0.0
    if hw is not None:
        # Lazy import to avoid a circular import at module load time.
        from aegis.atv.hw_encoders import fill_hw_band
        from aegis.hw_telemetry import HWCounters

        if not isinstance(hw, HWCounters):
            raise TypeError(
                f"build_atv: hw must be HWCounters or None, got {type(hw).__name__}"
            )
        fill_hw_band(atv, inp, hw)

    # paranoia: confirm we hit exactly 2080
    if atv.shape != (ATV_DIM,):
        raise AssertionError(f"build_atv produced shape {atv.shape}, expected ({ATV_DIM},)")
    return atv


# Keep a module-level reference to the canonical subfield list so callers
# (renderers, attribution heads, etc.) can iterate without re-importing.
__all__ = [
    "build_atv",
    "ALL_SUBFIELDS",
    # encoders (in case anyone wants to re-use them)
    "encode_agent_state_embedding",
    "encode_action_history",
    "encode_inter_agent_graph",
    "encode_memory_provenance",
    "encode_qom_scores",
    "encode_resource_access_pattern",
    "encode_prompt_structure",
    "encode_aid_ats_scalars",
    "encode_encryption_metadata",
    "encode_output_content_fingerprint",
    "encode_tool_arg_inspection",
    "encode_action_blast_radius",
    "encode_output_channel_diversity",
    "encode_session_behavioral_drift",
    "encode_mcp_trust_signals",
    "encode_grounding_metrics",
    "encode_novelty_score",
    "encode_human_oversight_state",
    "encode_cost_efficiency_metrics",
]
