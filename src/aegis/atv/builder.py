"""Build a 2080-D ATV from an ``ATVInput``.

Each band is filled by a dedicated encoder; the hardware band (idx
1880..2080) stays zero-filled in T2.
"""

from __future__ import annotations

import hashlib

import numpy as np

from aegis.atv.embeddings import get_provider
from aegis.schema import (
    ATV_DIM,
    SLICE_AGENT_STATE,
    SLICE_COST_EFFICIENCY,
    SLICE_HEADER,
    SLICE_MEMORY_FP,
    SLICE_PLAN,
    SLICE_SAFETY_FLAGS,
    SLICE_TOOL_CALL,
    ATVHeader,
    ATVInput,
)

SAFETY_FLAG_KEYS: list[str] = [
    "prompt_injection",
    "pii_exposure",
    "jailbreak",
    "toxicity",
    "sql_injection",
    "path_traversal",
    "data_exfiltration",
    "privilege_escalation",
    "credential_theft",
    "supply_chain",
    "ssrf",
    "xss",
    "command_injection",
    "deserialization",
    "rce",
    "model_extraction",
    "training_data_leak",
    "policy_bypass",
    "social_engineering",
    "rate_abuse",
    "spam",
    "phishing",
    "malware",
    "ransomware",
    "lateral_movement",
    "persistence",
    "exfil_network",
    "exfil_storage",
    "tampering",
    "denial_of_service",
    "resource_abuse",
    "anomaly_score",
]


def encode_header(h: ATVHeader) -> np.ndarray:
    """64-D deterministic encoding of the header fields via SHA3-256."""
    blob = f"{h.trace_id}|{h.span_id}|{h.tenant_id}|{h.aid}|{h.ats}|{h.timestamp_ns}"
    digest = hashlib.sha3_256(blob.encode("utf-8")).digest()  # 32 bytes
    arr = np.zeros(64, dtype=np.float32)
    for i, b in enumerate(digest):
        arr[2 * i] = ((b >> 4) - 7.5) / 7.5
        arr[2 * i + 1] = ((b & 0x0F) - 7.5) / 7.5
    return arr


def encode_safety_flags(flags: dict[str, float]) -> np.ndarray:
    """256-D safety flags vector. Known keys go to fixed slots; rest is zero."""
    arr = np.zeros(256, dtype=np.float32)
    for i, k in enumerate(SAFETY_FLAG_KEYS[:256]):
        arr[i] = float(flags.get(k, 0.0))
    return arr


def encode_memory_fp(fp: str | None) -> np.ndarray:
    """136-D deterministic encoding of an optional memory fingerprint string."""
    if not fp:
        return np.zeros(136, dtype=np.float32)
    digest = hashlib.sha3_512(fp.encode("utf-8")).digest()  # 64 bytes
    arr = np.zeros(136, dtype=np.float32)
    for i, b in enumerate(digest[:68]):
        arr[2 * i] = ((b >> 4) - 7.5) / 7.5
        arr[2 * i + 1] = ((b & 0x0F) - 7.5) / 7.5
    return arr


def build_atv(inp: ATVInput) -> np.ndarray:
    """Assemble the 2080-D ATV from the typed input."""
    emb = get_provider()
    atv = np.zeros(ATV_DIM, dtype=np.float32)
    atv[SLICE_HEADER] = encode_header(inp.header)
    atv[SLICE_AGENT_STATE] = emb.embed(inp.agent_state_text, 512)
    atv[SLICE_PLAN] = emb.embed(inp.plan_text, 512)
    atv[SLICE_TOOL_CALL] = emb.embed(f"{inp.tool_name}({inp.tool_args_json})", 384)
    atv[SLICE_SAFETY_FLAGS] = encode_safety_flags(inp.safety_flags)
    atv[SLICE_MEMORY_FP] = encode_memory_fp(inp.memory_fingerprint)
    atv[SLICE_COST_EFFICIENCY] = inp.cost_estimate.to_array()
    # Hardware band (1880..2080) intentionally remains zero in T2.
    return atv
