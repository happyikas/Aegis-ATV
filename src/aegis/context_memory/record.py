"""ContextMemoryRecord — the silicon-ready schema.

Every field here maps to a fixed-size cell in the eventual
CXL/Computational SSD layout. Strings are bounded; numerics are
plain int / float (no nested types beyond ``step_traces`` /
``recommended_advisors`` which decompose to row-major arrays at
silicon time). The reference encoding here is JSONL — readable,
git-diffable, and trivially convertible to the binary layout.

Schema version 1
----------------

::

    {
      "schema_version":         1,

      // identity ----------------------------------------------------
      "ts_ns":                  1714737610123456789,
      "trace_id":               "...",
      "invocation_id":          "...",
      "aid":                    "session-or-instance-id",
      "tenant_id":              "claude-code-local",

      // action -----------------------------------------------------
      "tool_name":              "Bash",

      // decision ---------------------------------------------------
      "decision":               "ALLOW" | "REQUIRE_APPROVAL" | "BLOCK",
      "reason":                 "<=300 char truncation",

      // routing ----------------------------------------------------
      "channel":                "telegram" | null,
      "provider":               "openrouter:anthropic-claude-sonnet-4" | null,

      // performance ------------------------------------------------
      "latency_ms":             47.3,

      // cost (best-effort attribution; 0.0 if unknown at write time)
      "cost_usd":               0.00038,
      "tokens_in":              412,
      "tokens_out":             86,

      // security ---------------------------------------------------
      "step_traces":            {"step310": "destructive bash", ...},
      "m13_score":              0.81,                  // sLLM confidence
      "advisor_invoked":        true,
      "recommended_advisors":   ["security-reviewer", "cost-optimizer"],

      // ATV fingerprint --------------------------------------------
      "atv_sha3":               "abc...",
      "atv_dim":                2080,

      // sidechain --------------------------------------------------
      "is_sidechain":           false,
      "mode":                   "local" | "sidecar"
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1
MAX_REASON_LEN = 300


@dataclass(frozen=True)
class ContextMemoryRecord:
    """One ATV analytics row. Frozen so writers can pass it freely.

    Construct via :meth:`from_audit_record` to derive from the
    existing audit-log dict, or via direct kwargs for tests.
    """

    ts_ns: int
    trace_id: str
    invocation_id: str
    aid: str
    tenant_id: str
    tool_name: str
    decision: str
    reason: str

    channel: str | None
    provider: str | None

    latency_ms: float

    cost_usd: float
    tokens_in: int
    tokens_out: int

    step_traces: dict[str, str]
    m13_score: float | None
    advisor_invoked: bool
    recommended_advisors: tuple[str, ...]

    atv_sha3: str | None
    atv_dim: int

    is_sidechain: bool
    mode: str

    schema_version: int = SCHEMA_VERSION

    # ── projection from / to dict ───────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ts_ns": self.ts_ns,
            "trace_id": self.trace_id,
            "invocation_id": self.invocation_id,
            "aid": self.aid,
            "tenant_id": self.tenant_id,
            "tool_name": self.tool_name,
            "decision": self.decision,
            "reason": self.reason,
            "channel": self.channel,
            "provider": self.provider,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "step_traces": dict(self.step_traces),
            "m13_score": self.m13_score,
            "advisor_invoked": self.advisor_invoked,
            "recommended_advisors": list(self.recommended_advisors),
            "atv_sha3": self.atv_sha3,
            "atv_dim": self.atv_dim,
            "is_sidechain": self.is_sidechain,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContextMemoryRecord:
        """Hydrate a record from a stored JSON dict — tolerant of
        missing optional fields for forward compatibility."""
        return cls(
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            ts_ns=int(d.get("ts_ns", 0)),
            trace_id=str(d.get("trace_id", "")),
            invocation_id=str(d.get("invocation_id", "")),
            aid=str(d.get("aid", "")),
            tenant_id=str(d.get("tenant_id", "")),
            tool_name=str(d.get("tool_name", "")),
            decision=str(d.get("decision", "")),
            reason=str(d.get("reason", ""))[:MAX_REASON_LEN],
            channel=_maybe_str(d.get("channel")),
            provider=_maybe_str(d.get("provider")),
            latency_ms=float(d.get("latency_ms", 0.0)),
            cost_usd=float(d.get("cost_usd", 0.0)),
            tokens_in=int(d.get("tokens_in", 0)),
            tokens_out=int(d.get("tokens_out", 0)),
            step_traces=dict(d.get("step_traces", {}) or {}),
            m13_score=_maybe_float(d.get("m13_score")),
            advisor_invoked=bool(d.get("advisor_invoked", False)),
            recommended_advisors=tuple(d.get("recommended_advisors", []) or ()),
            atv_sha3=_maybe_str(d.get("atv_sha3")),
            atv_dim=int(d.get("atv_dim", 0)),
            is_sidechain=bool(d.get("is_sidechain", False)),
            mode=str(d.get("mode", "")),
        )

    # ── projection from the audit record dict ───────────────────

    @classmethod
    def from_audit_record(
        cls, audit_rec: dict[str, Any], *, mode: str = "local",
    ) -> ContextMemoryRecord:
        """Project an audit-log record into ContextMemory shape.

        The audit record is the single richest dict in the system —
        firewall verdict, step traces, advisor pipeline output, ATV
        fingerprint all in one place. We pick out the analytics
        fields and drop the cryptographic chain fields (hash, sig,
        pubkey_fingerprint) since the silicon never sees those.

        Tolerant of missing fields — audit records vary slightly
        between local mode (`tools/aegis_local_hook.py`) and sidecar
        mode (`src/aegis/firewall/step360_audit.py`). Anything we
        can't find becomes a sensible default.
        """
        explain = audit_rec.get("explain") or {}
        cost = explain.get("cost") or audit_rec.get("cost_estimate") or {}
        advisor_gate = explain.get("advisor_gate") or {}
        action_advice = explain.get("action_advice") or {}

        # m13 attribution — stored as top-5 tuples in explain.m13_top,
        # but the overall confidence is explain.m13_score.
        m13_score_raw = explain.get("m13_score")
        m13_score: float | None
        try:
            m13_score = (
                float(m13_score_raw) if m13_score_raw is not None else None
            )
        except (TypeError, ValueError):
            m13_score = None

        # 8-advisor output — extract just the advisor names for cheap
        # bag-of-advisors analytics. Full action_steps stay in audit.
        recs_raw = action_advice.get("recommended_advisors") or []
        rec_names: list[str] = []
        for r in recs_raw:
            if isinstance(r, dict):
                name = r.get("advisor")
                if isinstance(name, str) and name:
                    rec_names.append(name)
            elif isinstance(r, str) and r:
                rec_names.append(r)

        # Step traces — already filtered + truncated by the hook;
        # keep as-is (≤200 chars per value enforced upstream).
        step_traces = audit_rec.get("step_traces")
        if not isinstance(step_traces, dict):
            step_traces = explain.get("step_traces") or {}
        step_traces = {
            str(k): str(v)[:200]
            for k, v in step_traces.items()
            if isinstance(k, str)
        }

        return cls(
            schema_version=SCHEMA_VERSION,
            ts_ns=int(audit_rec.get("ts_ns") or 0),
            trace_id=str(audit_rec.get("trace_id", "")),
            invocation_id=str(audit_rec.get("invocation_id", "")),
            aid=str(audit_rec.get("aid", "")),
            tenant_id=str(audit_rec.get("tenant_id", "") or ""),
            tool_name=str(audit_rec.get("tool", audit_rec.get("tool_name", ""))),
            decision=str(audit_rec.get("decision", "")),
            reason=str(audit_rec.get("reason", ""))[:MAX_REASON_LEN],
            channel=_maybe_str(audit_rec.get("channel")),
            provider=_maybe_str(audit_rec.get("provider")),
            latency_ms=float(audit_rec.get("latency_ms") or 0.0),
            cost_usd=_safe_float(cost.get("total_usd", cost.get("usd", 0.0))),
            tokens_in=_safe_int(cost.get("tokens_in", 0)),
            tokens_out=_safe_int(cost.get("tokens_out", 0)),
            step_traces=step_traces,
            m13_score=m13_score,
            advisor_invoked=bool(advisor_gate.get("invoked", False)),
            recommended_advisors=tuple(rec_names),
            atv_sha3=_maybe_str(explain.get("atv_sha3")),
            atv_dim=_safe_int(explain.get("atv_dim", 0)),
            is_sidechain=bool(audit_rec.get("is_sidechain", False)),
            mode=str(audit_rec.get("mode", mode)),
        )


# ── tiny coercion helpers ────────────────────────────────────────


def _maybe_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _maybe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_float(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe_int(v: Any) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


__all__ = [
    "MAX_REASON_LEN",
    "SCHEMA_VERSION",
    "ContextMemoryRecord",
]
