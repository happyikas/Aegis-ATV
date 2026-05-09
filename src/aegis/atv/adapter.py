"""Claude Code hook payload → :class:`ATVInput` adapter (Phase 3, v2.0).

Phase 3 of INTEGRATION_PLAN: bridges the aegis-mvp v1.0.0 plugin shape
(``{session_id, tool_name, tool_input, ...}``) into MVP/'s
ATV-2080-v1 30-subfield :class:`aegis.schema.ATVInput` so the same
sidecar ``/evaluate`` endpoint serves both deployment modes.

Design notes:

* The 256-D donor encoder (``aegis-mvp/atv/encoder.py``) is *not*
  embedded into the 2080-D ATV. Per INTEGRATION_PLAN §6 "ATV 256-d →
  2080-d 매핑이 의미 손실" risk, mapping the donor's flat hashed
  n-gram bag into a specific subfield is deferred — MVP/'s firewall
  has its own per-subfield encoders (see ``aegis.atv.builder``) and
  re-encoding the same payload twice would double-count features.
  The donor encoder is preserved verbatim under
  :func:`donor_behavior_features` for callers that want a 32-D
  hand-engineered feature vector (the deterministic part of
  ``aegis-mvp/atv/encoder.encode``).
* :func:`from_claude_code_payload` accepts both the raw Claude Code
  PreToolUse payload (``tool_name``/``tool_input``/``session_id``) and
  the legacy ``{tool, args, agent_id}`` shape. It funnels through
  ``aegis_payload.normalize_input`` so the contract stays identical
  to the live hook.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

import numpy as np

from aegis.schema import ATVHeader, ATVInput

# Donor encoder regexes (preserved verbatim from aegis-mvp/atv/encoder.py).
_DESTRUCTIVE_KW = re.compile(
    r"\b(rm|drop|truncate|delete|kill|--force|-rf|format|wipe)\b",
    re.IGNORECASE,
)
_NETWORK_KW = re.compile(
    r"\b(curl|wget|fetch|http|post|send|exfil)\b", re.IGNORECASE
)
_PRIVILEGE_KW = re.compile(
    r"\b(sudo|root|admin|chmod|setuid|capability)\b", re.IGNORECASE
)
_SECRET_KW = re.compile(
    r"(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{36})"
)
_SQL_KW = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|TRUNCATE)\b", re.IGNORECASE
)
_GIT_KW = re.compile(r"\bgit\s+(push|rebase|reset|branch)\b", re.IGNORECASE)
_PATH_TRAV = re.compile(r"\.\./|/etc/|/root/|~/\.")
_PROMPT_INJ = re.compile(
    r"ignore (previous|prior) (instruction|prompt)", re.IGNORECASE
)


def _normalize_payload(req: dict[str, Any]) -> dict[str, Any]:
    """Return ``{tool, args, aid, invocation_id, cwd, session_id, mode}``.

    Mirrors :func:`tools.aegis_payload.normalize_input` so the adapter
    stays decoupled from the package layout (``tools/`` is not always
    importable in mypy / wheel-only contexts).
    """
    if "tool_name" in req:
        return {
            "tool": req.get("tool_name", ""),
            "args": req.get("tool_input", {}) or {},
            "aid": req.get("session_id", "default"),
            "invocation_id": req.get("invocation_id") or uuid.uuid4().hex,
            "cwd": req.get("cwd", ""),
            "session_id": req.get("session_id", ""),
            "mode": "claude_code",
        }
    return {
        "tool": req.get("tool", ""),
        "args": req.get("args", {}) or {},
        "aid": req.get("agent_id", "default"),
        "invocation_id": req.get("invocation_id") or uuid.uuid4().hex,
        "cwd": req.get("cwd", ""),
        "session_id": req.get("session_id", ""),
        "mode": "legacy",
    }


def _trace_ids_from(invocation_id: str) -> tuple[str, str]:
    """Derive (trace_id, span_id) deterministically from an invocation id.

    Using a stable derivation means two evaluations of the same Claude
    Code tool call produce the same audit anchor, which lets ``aegis
    replay`` and ``aegis verify-audit`` deduplicate by trace.
    """
    h = hashlib.sha3_256(invocation_id.encode("utf-8")).hexdigest()
    return h[:32], h[32:48]


def from_claude_code_payload(
    req: dict[str, Any],
    *,
    tenant_id: str = "claude-code",
    role_id: str | None = None,
    agent_state_text: str = "",
    plan_text: str = "",
) -> ATVInput:
    """Build an :class:`ATVInput` from a Claude Code hook payload.

    Accepts both the canonical Claude Code shape (``tool_name`` /
    ``tool_input`` / ``session_id``) and the legacy
    ``{tool, args, agent_id}`` shape. Optional kwargs let the caller
    inject SW-band context that PreToolUse doesn't carry (role, the
    agent's own goal text, the current plan).

    This is the **v4.4 sparse adapter** — only header + tool fields
    populated. For richer context including transcript-derived
    ``agent_state_text``, ``recent_actions``, ``cost_estimate``,
    ``novelty_score`` etc., use
    :func:`from_claude_code_payload_enhanced` instead.
    """
    norm = _normalize_payload(req)

    trace_id, span_id = _trace_ids_from(str(norm["invocation_id"]))
    aid_value = norm["aid"] or "default"
    args_payload = norm["args"]

    header = ATVHeader(
        trace_id=trace_id,
        span_id=span_id,
        tenant_id=tenant_id,
        aid=str(aid_value),
        timestamp_ns=time.time_ns(),
    )

    return ATVInput(
        header=header,
        agent_state_text=agent_state_text,
        role_id=role_id,
        plan_text=plan_text,
        tool_name=str(norm["tool"]) or "unknown",
        tool_args_json=json.dumps(args_payload, sort_keys=True, default=str),
    )


def from_claude_code_payload_enhanced(
    req: dict[str, Any],
    *,
    tenant_id: str = "claude-code",
    role_id: str | None = None,
    model_for_cost: str = "claude-haiku-4-5",
) -> ATVInput:
    """Plug-in checkup v1 — transcript-aware ATV builder.

    Same contract as :func:`from_claude_code_payload` but additionally
    reads ``transcript_path`` (when present) and populates 6+ more
    subfields:

    * ``agent_state_text`` ← last assistant message
    * ``plan_text`` ← extracted plan line
    * ``recent_actions`` ← last 20 tool calls
    * ``memory_fingerprint`` ← SHA3-256 of transcript bytes
    * ``cost_estimate`` ← cumulative tokens / dollars from transcript
    * ``novelty.composite_novelty`` ← Jaccard distance vs recent calls
    * ``session_behavior`` ← Bash / Edit / Read call density
    * ``mcp_context`` ← MCP tool call ratio
    * ``oversight.operator_presence`` ← TTY / env detection

    Falls back to the v4.4 sparse builder when transcript is missing
    or unparseable.
    """
    base = from_claude_code_payload(
        req, tenant_id=tenant_id, role_id=role_id,
    )

    transcript_path = req.get("transcript_path", "")
    if not transcript_path:
        return base

    from aegis.atv.transcript_reader import (
        operator_present_from_env,
        read_transcript_context,
    )

    ctx = read_transcript_context(
        transcript_path,
        next_tool_args_json=base.tool_args_json,
        model_for_cost=model_for_cost,
    )
    if ctx is None:
        return base

    # Step340-prep: BGE-derived behavioural-drift signals.
    # Computes ``topic_drift`` (cosine distance to session anchor) and
    # ``verbosity_drift`` (z-score of plan_text length) when both:
    # (1) a session_id is present in the Claude Code payload, and
    # (2) the embedding provider is bge-local (otherwise the cosine
    #     is meaningless SHA3 noise).
    # Returns zeros and stays out of the way otherwise — same shape
    # as the pre-PR-#25 dict, plus the two new keys.
    drift_keys = _maybe_compute_session_drift(
        session_id=str(req.get("session_id") or ""),
        agent_state_text=ctx.last_assistant_message,
        plan_text=ctx.current_plan or base.plan_text,
    )
    # PR-A subagent attribution — surface as numeric session_behavior
    # signals so existing dashboards / step340 / `aegis report` see them
    # without needing a schema bump.
    subagent_keys: dict[str, float] = {
        "sidechain_is_active": 1.0 if ctx.current_event_is_sidechain else 0.0,
        "sidechain_event_count": float(ctx.sidechain_event_count),
        "sidechain_tool_call_count": float(ctx.sidechain_tool_call_count),
    }
    merged_session_behavior = {**ctx.behavior_metrics, **drift_keys, **subagent_keys}

    return base.model_copy(update={
        "agent_state_text": ctx.last_assistant_message,
        "plan_text": ctx.current_plan or base.plan_text,
        "recent_actions": ctx.recent_tool_calls,
        "memory_fingerprint": ctx.transcript_sha3,
        "cost_estimate": ctx.cumulative_cost,
        "novelty": {"composite_novelty": ctx.novelty_score},
        "session_behavior": merged_session_behavior,
        "mcp_context": ctx.mcp_signals,
        "oversight": {"operator_presence": operator_present_from_env()},
    })


def _maybe_compute_session_drift(
    *, session_id: str, agent_state_text: str, plan_text: str,
) -> dict[str, float]:
    """Compute (topic_drift, verbosity_drift) when BGE is configured.

    Wrapped in a try/except so any disk / embedding error degrades to
    an empty dict — caller merges it with ``ctx.behavior_metrics``,
    so an empty dict means the slot stays at its pre-PR-#25 zeros.
    Never raises.
    """
    try:
        from aegis.config import settings
        if settings.aegis_embedding_provider != "bge-local":
            return {}
        if not session_id or not agent_state_text:
            return {}

        from aegis.atv.embeddings import get_provider
        from aegis.atv.session_drift import update_and_score

        # Reuse the same provider the encoder uses (BGE-local, real-mode).
        # Falls back to dummy internally on missing GGUF — in that case
        # `update_and_score` still runs but the cosine has no semantic
        # meaning. Detection above (provider != "bge-local") rules that
        # out so we only get here when bge-local is selected.
        embedding = get_provider().embed(agent_state_text, 768)
        signals = update_and_score(
            session_id=session_id,
            current_embedding=embedding,
            current_plan_len=len(plan_text or ""),
        )
        return signals.to_session_behavior()
    except Exception:  # noqa: BLE001 — drift must never break the firewall
        return {}


def donor_behavior_features(tool: str, args: dict[str, Any]) -> np.ndarray:
    """Return the donor encoder's 32-D hand-engineered feature vector.

    Preserved verbatim from aegis-mvp v1.0.0 ``atv/encoder.py``. Useful
    for callers that want to seed an anomaly classifier with the same
    deterministic feature shape the donor used during its 12-incident
    KPI; the MVP firewall already extracts richer signals into the
    ATV-2080 ``tool_arg_inspection`` and ``action_blast_radius``
    subfields, so this is informational only and is **not** wired into
    :func:`from_claude_code_payload` by default.
    """
    text = f"{tool} " + " ".join(str(v) for v in args.values())
    f = np.zeros(32, dtype=np.float32)
    cat = {
        "Read": 0,
        "Write": 1,
        "Edit": 2,
        "Bash": 3,
        "shell": 3,
        "bash": 3,
        "exec": 3,
        "fetch": 4,
        "http_request": 4,
        "curl": 4,
        "sql": 5,
        "execute_sql": 5,
        "git": 6,
    }.get(tool, 7)
    f[cat] = 1.0
    f[8] = min(len(args) / 10.0, 1.0)
    text_len = len(text)
    f[9] = min(float(np.log1p(text_len)) / 10.0, 1.0)
    f[10] = 1.0 if _DESTRUCTIVE_KW.search(text) else 0.0
    f[11] = 1.0 if _NETWORK_KW.search(text) else 0.0
    f[12] = 1.0 if _PRIVILEGE_KW.search(text) else 0.0
    f[13] = 1.0 if _SECRET_KW.search(text) else 0.0
    f[14] = 1.0 if _SQL_KW.search(text) else 0.0
    f[15] = 1.0 if _GIT_KW.search(text) else 0.0
    f[16] = 1.0 if _PATH_TRAV.search(text) else 0.0
    f[17] = 1.0 if "http://" in text or "https://" in text else 0.0
    f[18] = 1.0 if _PROMPT_INJ.search(text) else 0.0
    f[19] = min(text.count("/") / 20.0, 1.0)
    wc = len(text.split())
    f[20] = min(wc / 100.0, 1.0)
    f[21] = sum(1 for c in text if c.isupper()) / max(text_len, 1)
    f[22] = sum(1 for c in text if c.isdigit()) / max(text_len, 1)
    f[23] = sum(1 for c in text if c in "{}[]<>") / max(text_len, 1)
    f[24] = 1.0 if "rm" in text.split() else 0.0
    f[25] = 1.0 if "--force" in text or "-f " in text else 0.0
    f[26] = (
        1.0 if any(k in text.lower() for k in (".tk", ".ml", ".cf", ".gq")) else 0.0
    )
    f[27] = min(text.count("|") / 5.0, 1.0)
    n = max(len(args), 1)
    f[28] = sum(1 for v in args.values() if isinstance(v, str)) / n
    f[29] = sum(1 for v in args.values() if isinstance(v, int | float)) / n
    f[30] = sum(1 for v in args.values() if isinstance(v, list | dict)) / n
    f[31] = 1.0 if tool.lower().startswith(("mcp__", "mcp:")) else 0.0
    return f
