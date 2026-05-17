"""Autonomy runtime — trust table persistence + Verdict bypass shim.

This module sits between the firewall and the hook surface. After
``run_firewall`` produces a Verdict, the hook calls
:func:`apply_autonomy_bypass` to consult the learned trust table.
If the verdict is REQUIRE_APPROVAL **and** matches a high-trust
pattern, the Verdict is downgraded to ALLOW with a permanent
``aegis.autonomy.step331.run`` stamp in ``step_traces`` so the
audit chain captures the bypass.

The hook side is opt-in via ``AEGIS_AUTONOMY_ENABLED=1``; when the
flag is off, :func:`apply_autonomy_bypass` returns the verdict
unchanged, preserving byte-identical legacy behaviour.

v0.5.12 additions
-----------------

* **ε-greedy forced exploration.** Even when a pattern is fully
  trusted, a deterministic fraction ``ε`` of matching calls still
  go to the human (no bypass). This breaks the self-confirming
  loop where active bypass suppresses the BLOCK / deny signal we
  need to detect drift. ``ε`` is controlled by
  ``AEGIS_AUTONOMY_EPSILON`` (default 0.05 = 5%). Reproducible:
  the per-call decision is BLAKE2b of the verdict's ``atv_id``
  mod 100, so replay produces identical bypass / explore choices.
* **Drift refusal** — the Verdict shim consults
  :func:`evaluate_autonomy_request` whose v0.5.12 implementation
  rejects drifted patterns regardless of trust score. No extra
  code here; the safeguard happens through the learner contract.

Trust table on disk
-------------------

Persisted to ``~/.aegis/autonomy/trust_table.json`` (override via
``AEGIS_AUTONOMY_TRUST_TABLE`` env). v0.5.12 adds posterior +
drift fields to each pattern entry; v0.5.11 readers will still
parse the file (extra fields are ignored).

Re-learning is explicit (``aegis autonomy learn``); the trust
table never auto-evolves at runtime. This preserves the audit
property that "the autonomy decisions in this window come from
this exact trust table snapshot".
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from aegis.autonomy.learner import (
    MIN_TRUST_FOR_BYPASS,
    AutonomyVerdict,
    TrustedPattern,
    autonomy_enabled,
    evaluate_autonomy_request,
)
from aegis.schema import Verdict

# Stamp the firewall writes into step_traces on every bypass. The
# outlier walker keys on this prefix; keep the format stable.
STEP_TRACE_KEY = "aegis.autonomy.step331.run"
STEP_TRACE_PREFIX = "step331: auto-approved"

# Stamp written when ε-greedy chose to NOT bypass even though the
# pattern is trusted. The outlier walker is *not* interested in
# these (they kept the human in the loop, so by definition can't
# be auto-approval outliers), but ``aegis autonomy show`` reports
# the rate so the operator can see exploration is actually
# happening.
STEP_TRACE_EXPLORE_KEY = "aegis.autonomy.step331.explore"
STEP_TRACE_EXPLORE_PREFIX = "step331: forced exploration"

# Default ε for forced exploration. 5% = ~1 in 20 trusted-pattern
# matches still go to the human. Empirically this is small enough
# to barely disturb the operator and large enough to drive a
# steady stream of fresh CLEAN observations for drift detection.
DEFAULT_EPSILON: float = 0.05


def trust_table_path() -> Path:
    """Return the canonical on-disk path for the trust table."""
    raw = os.environ.get("AEGIS_AUTONOMY_TRUST_TABLE", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".aegis" / "autonomy" / "trust_table.json"


def save_trust_table(
    table: dict[tuple[str, str], TrustedPattern],
    *,
    path: Path | None = None,
    learned_from_records: int = 0,
    min_samples: int = 0,
    min_clean_rate: float = 0.0,
) -> Path:
    """Persist the trust table to disk. Returns the path written.

    Atomic write via tempfile + rename so a concurrent reader
    never sees a partial file. The metadata fields (learned_at,
    learned_from_records, …) let the show/outliers commands
    explain *when* the table was built without re-mining."""
    target = path if path is not None else trust_table_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "learned_at": datetime.now(UTC).isoformat(),
        "learned_from_records": int(learned_from_records),
        "min_samples": int(min_samples),
        "min_clean_rate": float(min_clean_rate),
        "patterns": [asdict(p) for p in table.values()],
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(target)
    return target


def load_trust_table(
    path: Path | None = None,
) -> dict[tuple[str, str], TrustedPattern]:
    """Read the trust table from disk. Returns an empty dict if
    the file doesn't exist or is malformed — both cases imply
    "no bypass" which is the safe default.

    v0.5.12: the loader is forward+backward compatible. v0.5.11
    entries (no posterior fields) load with default ``alpha=0``
    so :meth:`TrustedPattern.posterior` derives one from integer
    counts on first use. v0.5.13+ fields (not present here yet)
    are silently ignored if encountered."""
    target = path if path is not None else trust_table_path()
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    patterns_list = payload.get("patterns", [])
    if not isinstance(patterns_list, list):
        return {}
    out: dict[tuple[str, str], TrustedPattern] = {}
    for raw in patterns_list:
        if not isinstance(raw, dict):
            continue
        try:
            p = TrustedPattern(
                tool_name=str(raw["tool_name"]),
                reason_signature=str(raw["reason_signature"]),
                n_seen=int(raw["n_seen"]),
                n_followed_by_block=int(raw["n_followed_by_block"]),
                clean_rate=float(raw["clean_rate"]),
                trust_score=float(raw["trust_score"]),
                last_seen_ns=int(raw["last_seen_ns"]),
                sample_trace_ids=tuple(raw.get("sample_trace_ids", []) or ()),
                # v0.5.12 — Bayesian posterior + drift. Defaults
                # of 0.0 / False / DEFAULT_CREDIBILITY ensure
                # v0.5.11 entries still produce a usable
                # TrustedPattern (LCB derived from integer counts).
                alpha=float(raw.get("alpha", 0.0) or 0.0),
                beta=float(raw.get("beta", 0.0) or 0.0),
                posterior_mean=float(raw.get("posterior_mean", 0.0) or 0.0),
                posterior_std=float(raw.get("posterior_std", 0.0) or 0.0),
                n_effective=float(raw.get("n_effective", 0.0) or 0.0),
                n_explicit_deny=int(raw.get("n_explicit_deny", 0) or 0),
                drift_score=float(raw.get("drift_score", 0.0) or 0.0),
                drifted=bool(raw.get("drifted", False)),
                credibility=float(
                    raw.get("credibility", 0.95) or 0.95
                ),
                prior_alpha=float(raw.get("prior_alpha", 1.0) or 1.0),
                prior_beta=float(raw.get("prior_beta", 5.0) or 5.0),
                # v0.5.23 — runtime-fingerprint centroid + cov_diag.
                # v0.5.22 and earlier entries have these as empty
                # tuples (no Mahalanobis gating).
                atv_centroid=tuple(
                    float(x) for x in (raw.get("atv_centroid") or [])
                ),
                atv_cov_diag=tuple(
                    float(x) for x in (raw.get("atv_cov_diag") or [])
                ),
                centroid_n_samples=int(
                    raw.get("centroid_n_samples", 0) or 0,
                ),
            )
        except (KeyError, TypeError, ValueError):
            continue
        out[p.key] = p
    return out


def trust_table_metadata(
    path: Path | None = None,
) -> dict[str, object]:
    """Return the on-disk metadata (learned_at, sample counts, …)
    without re-deserialising patterns. Empty dict on missing file."""
    target = path if path is not None else trust_table_path()
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        k: payload.get(k) for k in (
            "learned_at", "learned_from_records",
            "min_samples", "min_clean_rate",
        )
    }


def _epsilon_from_env(default: float = DEFAULT_EPSILON) -> float:
    """Read the exploration rate from the environment.

    Returns a value in ``[0.0, 0.5]``. Caller never has to clamp;
    out-of-range / unparseable values fall back to the default.
    A 50% cap stops a misconfiguration from suppressing autonomy
    altogether — at that point the operator should set
    ``AEGIS_AUTONOMY_ENABLED=0`` instead."""
    raw = os.environ.get("AEGIS_AUTONOMY_EPSILON", "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    if v < 0.0 or v > 0.5:
        return default
    return v


def _should_explore(*, atv_id: str, epsilon: float) -> bool:
    """Deterministic ε-greedy decision: returns True iff this
    specific verdict should be forced to ask a human despite
    matching a trusted pattern.

    Determinism via BLAKE2b(atv_id) mod 1000 means:

    * Replaying the same audit trail reproduces the identical
      bypass / explore choices.
    * The choice is content-addressed by the verdict's atv_id
      so no random source is needed at runtime.
    * Resolution of 1/1000 lets ε be tuned in 0.1% steps."""
    if epsilon <= 0.0 or not atv_id:
        return False
    digest = hashlib.blake2b(atv_id.encode("utf-8"), digest_size=8).digest()
    bucket = int.from_bytes(digest, "big") % 1000
    threshold = int(epsilon * 1000)
    return bucket < threshold


def apply_autonomy_bypass(
    verdict: Verdict,
    *,
    tool_name: str,
    reason: str,
    trust_table: dict[tuple[str, str], TrustedPattern] | None = None,
    min_trust: float = MIN_TRUST_FOR_BYPASS,
    epsilon: float | None = None,
    tool_args_json: str = "",
    runtime_features: tuple[float, ...] | None = None,
) -> tuple[Verdict, AutonomyVerdict]:
    """Consult the trust table; downgrade REQUIRE_APPROVAL to
    ALLOW when a high-trust pattern matches.

    Returns ``(new_verdict, autonomy_verdict)``. ``new_verdict``
    is either the original ``verdict`` (no bypass) or a copy with
    ``decision = "ALLOW"`` plus an additional step_trace entry
    keyed :data:`STEP_TRACE_KEY`. The ``autonomy_verdict`` carries
    the AutonomyVerdict diagnostic for forensics / logging
    regardless of which path was taken.

    v0.5.12: even when the trust table says ``auto_approve=True``,
    a deterministic ε-fraction of calls are forced back to the
    human (ε-greedy exploration). The forced-explore decision
    leaves the verdict unchanged but stamps
    :data:`STEP_TRACE_EXPLORE_KEY` so the operator can see how
    often exploration is firing. ``epsilon=None`` reads from
    ``AEGIS_AUTONOMY_EPSILON`` (default 0.05). Pass ``epsilon=0.0``
    to disable exploration entirely (used by tests).

    Never raises. When ``AEGIS_AUTONOMY_ENABLED`` is unset, the
    function short-circuits at the top and returns ``(verdict,
    ask_human_verdict)`` without touching the trust table.
    """
    # Short-circuit when the operator hasn't opted in.
    if not autonomy_enabled():
        return verdict, AutonomyVerdict(
            auto_approve=False,
            matched_pattern=None,
            confidence=0.0,
            reason="AEGIS_AUTONOMY_ENABLED is off",
        )

    # Only REQUIRE_APPROVAL verdicts are candidates for bypass.
    if verdict.decision != "REQUIRE_APPROVAL":
        return verdict, AutonomyVerdict(
            auto_approve=False,
            matched_pattern=None,
            confidence=0.0,
            reason="verdict is not REQUIRE_APPROVAL",
        )

    # v0.5.22 — reversibility hard gate. Irreversible actions
    # (rm -rf, force-push, kubectl delete, package publish, …)
    # NEVER auto-bypass regardless of trust score, drift, or
    # ε-greedy. This is the operator's principled safety floor
    # — independent of the statistical trust learner.
    try:
        from aegis.policies.reversibility import (
            classify_reversibility,
        )
        revcls = classify_reversibility(tool_name, tool_args_json)
        if revcls.level == "irreversible":
            return verdict, AutonomyVerdict(
                auto_approve=False,
                matched_pattern=None,
                confidence=0.0,
                reason=(
                    "reversibility=irreversible — never auto-bypassed "
                    f"(matched: {revcls.why!r})"
                ),
                outlier_signals=("irreversible_action",),
            )
    except Exception:  # noqa: BLE001 — never raise from hot path
        pass

    table = trust_table if trust_table is not None else load_trust_table()
    av = evaluate_autonomy_request(
        tool_name=tool_name,
        reason=reason,
        trust_table=table,
        min_trust=min_trust,
        runtime_features=runtime_features,
    )
    if not av.auto_approve:
        return verdict, av

    # ε-greedy: even a trusted pattern is forced to ask the human
    # once in a while so the off-policy distribution shift doesn't
    # silence our drift / negative-reward signal.
    eps = epsilon if epsilon is not None else _epsilon_from_env()
    if _should_explore(atv_id=verdict.atv_id, epsilon=eps):
        explore_stamp = (
            f"{STEP_TRACE_EXPLORE_PREFIX} (ε={eps:.3f}) — "
            f"trust score {av.confidence:.2f} above threshold but "
            "forcing human review for drift / IPW coverage"
        )
        new_traces = dict(verdict.step_traces)
        new_traces[STEP_TRACE_EXPLORE_KEY] = explore_stamp
        explored_verdict = Verdict(
            decision=verdict.decision,
            reason=verdict.reason,
            atv_id=verdict.atv_id,
            signature=verdict.signature,
            confidence=verdict.confidence,
            step_traces=new_traces,
            step_timings_us=verdict.step_timings_us,
        )
        return explored_verdict, AutonomyVerdict(
            auto_approve=False,
            matched_pattern=av.matched_pattern,
            confidence=av.confidence,
            reason=(
                f"ε-greedy forced exploration (ε={eps:.3f}) — pattern "
                f"would have been bypassed; kept human in loop for "
                "drift coverage"
            ),
            outlier_signals=("forced_exploration",),
        )

    # Build the new verdict — ALLOW with a stamped step_traces.
    assert av.matched_pattern is not None  # narrow for type checker
    stamp = (
        f"{STEP_TRACE_PREFIX} by trust table "
        f"tool={tool_name} signature={av.matched_pattern.reason_signature} "
        f"trust={av.confidence:.2f} "
        f"(was REQUIRE_APPROVAL: {verdict.reason!r})"
    )
    new_traces = dict(verdict.step_traces)
    new_traces[STEP_TRACE_KEY] = stamp
    new_verdict = Verdict(
        decision="ALLOW",
        reason=(
            f"auto-approved by autonomy bypass — pattern "
            f"{av.matched_pattern.reason_signature!r} trusted at "
            f"LCB {av.confidence:.2f}"
        ),
        atv_id=verdict.atv_id,
        signature=verdict.signature,
        confidence=verdict.confidence,
        step_traces=new_traces,
        step_timings_us=verdict.step_timings_us,
    )
    return new_verdict, av


__all__ = [
    "DEFAULT_EPSILON",
    "STEP_TRACE_EXPLORE_KEY",
    "STEP_TRACE_EXPLORE_PREFIX",
    "STEP_TRACE_KEY",
    "STEP_TRACE_PREFIX",
    "apply_autonomy_bypass",
    "load_trust_table",
    "save_trust_table",
    "trust_table_metadata",
    "trust_table_path",
]
