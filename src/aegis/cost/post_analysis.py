"""PostToolUse content analysis — derive inefficiency signals from
the tool result without storing the raw response body.

Signals exposed
---------------

1. **Classification** (``classify_response``):
   ``size_bytes`` / ``line_count`` / ``is_empty`` / ``is_error`` /
   ``has_url`` / ``has_path`` / ``has_traceback`` — derived from the
   shape and content of ``tool_response``. Cheap regex over the
   serialised body. Never stores the body itself.

2. **Backtrack** (``detect_backtrack``): when an Edit / Write tool
   call's ``new_string`` matches the ``old_string`` of a recent
   Edit on the same file, that's a revert (the agent is undoing its
   own work). Walks the last N audit records.

3. **Redundancy** (``detect_redundant_call``): same tool + same
   args_hash within the last N records → repeat work.

4. **Duration** (``compute_duration_ms``): Pre→Post wall-clock from
   the ATMU intent log's ``created_at_ns`` (PR #31 deterministic
   record_id makes this zero-overhead).

Privacy posture
---------------

By default we record **only metadata** — sizes, counts, classification
flags, structural hashes. The raw ``tool_response`` body never lands
in the audit chain (only its SHA3 commitment, as before). Set
``AEGIS_POST_CAPTURE_PREVIEW=1`` to additionally record the first
80 chars of the response for debugging — opt-in because the body
can contain secrets / PII / customer data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Audit lookback for backtrack/redundancy detection.
DEFAULT_BACKTRACK_LOOKBACK: int = 50

# Toggle response preview capture (default OFF — privacy).
PREVIEW_ENABLED_ENV: str = "AEGIS_POST_CAPTURE_PREVIEW"
PREVIEW_MAX_CHARS: int = 80

# Compiled patterns — cheap reuse.
_URL_RE = re.compile(r"https?://[^\s<>\")]+")
_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._\-~]+){2,}")
_TRACEBACK_RE = re.compile(
    r"\bTraceback\b|\bException\b|\bError:\b", re.IGNORECASE
)


@dataclass
class ResponseClassification:
    """Per-call result classification — gets serialised into the
    audit explain block as a flat dict."""

    size_bytes: int = 0
    line_count: int = 0
    is_empty: bool = False
    is_error: bool = False
    has_url: bool = False
    has_path: bool = False
    has_traceback: bool = False
    preview: str | None = None    # opt-in via AEGIS_POST_CAPTURE_PREVIEW


@dataclass
class BacktrackEvidence:
    """Evidence that this Edit / Write reverts an earlier one."""

    reverted_trace_id: str
    file_path: str
    matched_string_hash: str       # SHA3-256 prefix (16 chars)


@dataclass
class PostAnalysis:
    """All PostToolUse signals — flat container for the audit explain block."""

    classification: ResponseClassification = field(
        default_factory=ResponseClassification
    )
    duration_ms: float | None = None      # PR #31 intent_log lookup
    backtrack: BacktrackEvidence | None = None
    redundant_of: str | None = None       # trace_id of prior identical call


# ─────────────────────────────────────────────────────────────────────
# 1. Response classification
# ─────────────────────────────────────────────────────────────────────


def _serialised_body(tool_response: Any) -> str:
    """Best-effort string view of the response for regex / size."""
    if tool_response is None:
        return ""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        # Common Claude Code shapes:
        # {"stdout": "...", "stderr": "...", "interrupted": False, ...}
        # {"content": "...", ...}
        # {"is_error": True, "error": "..."}
        for key in ("content", "stdout", "text", "output", "error"):
            v = tool_response.get(key)
            if isinstance(v, str):
                return v
        try:
            return json.dumps(tool_response, default=str)
        except (TypeError, ValueError):
            return repr(tool_response)
    try:
        return json.dumps(tool_response, default=str)
    except (TypeError, ValueError):
        return repr(tool_response)


def _is_error_response(tool_response: Any, exit_code: int | None) -> bool:
    if exit_code is not None and exit_code != 0:
        return True
    if isinstance(tool_response, dict):
        if tool_response.get("is_error") is True:
            return True
        if tool_response.get("error"):
            return True
    return False


def classify_response(
    tool_response: Any,
    *,
    exit_code: int | None = None,
    capture_preview: bool | None = None,
) -> ResponseClassification:
    """Derive classification flags from the tool's response without
    persisting the body. Cheap (one regex pass over the serialised
    string) so it's safe in the PostToolUse hot path."""
    body = _serialised_body(tool_response)
    size = len(body.encode("utf-8")) if body else 0
    lines = body.count("\n") + 1 if body else 0

    cls = ResponseClassification(
        size_bytes=size,
        line_count=lines,
        is_empty=size == 0,
        is_error=_is_error_response(tool_response, exit_code),
        has_url=bool(_URL_RE.search(body)) if body else False,
        has_path=bool(_PATH_RE.search(body)) if body else False,
        has_traceback=bool(_TRACEBACK_RE.search(body)) if body else False,
    )

    # Optional preview — disabled by default for privacy. Both the
    # explicit ``capture_preview`` arg and the env var must agree.
    env_on = os.environ.get(PREVIEW_ENABLED_ENV, "0") in ("1", "true", "True", "yes")
    do_capture = env_on if capture_preview is None else capture_preview
    if do_capture and body:
        snippet = body[:PREVIEW_MAX_CHARS]
        if len(body) > PREVIEW_MAX_CHARS:
            snippet += "…"
        cls.preview = snippet
    return cls


# ─────────────────────────────────────────────────────────────────────
# 2. Backtrack detection
# ─────────────────────────────────────────────────────────────────────


def _stable_string_hash(s: str) -> str:
    return hashlib.sha3_256(s.encode("utf-8")).hexdigest()[:16]


def _walk_audit_records_reverse(
    audit_path: Path, lookback: int,
) -> list[dict[str, Any]]:
    """Read the last ``lookback`` non-empty JSON lines of ``audit_path``
    in reverse chronological order (most recent first). Returns []
    if the file is missing or unreadable."""
    if not audit_path.is_file():
        return []
    try:
        # For typical audit files (<10MB) reading line-by-line is fine.
        # For very large files this would benefit from a tail-from-end
        # implementation; deferred until a real perf issue surfaces.
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
        if len(out) >= lookback:
            break
    return out


def detect_backtrack(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    audit_path: Path,
    lookback: int = DEFAULT_BACKTRACK_LOOKBACK,
) -> BacktrackEvidence | None:
    """If the current Edit/Write reverts a previous one on the same
    file, return evidence; else None.

    Detection heuristic
    -------------------

    For Edit-family tools, an "agent revert" is when this call's
    ``old_string`` (the text being removed) was previously
    *inserted* by an earlier call — i.e., a prior Edit's
    ``new_string`` matches our ``old_string`` for the same path.

    We hash the matched string so the detection survives regardless
    of whether the audit chain stored the raw text (it doesn't).
    """
    if tool_name not in ("Edit", "MultiEdit", "Write"):
        return None
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return None

    # Pull the candidate "I am inserting THIS" string from the current call.
    if tool_name == "Edit":
        my_old = tool_input.get("old_string", "") or ""
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        # Just use the first edit for now — extend if needed.
        my_old = (
            (edits[0].get("old_string") or "")
            if edits and isinstance(edits[0], dict) else ""
        )
    else:  # Write
        # Write replacing existing → reverting if the OLD content (which we
        # don't have access to in tool_input) matched a prior new. We
        # don't have enough info from tool_input alone to detect Write
        # backtracks reliably, so skip Write here. Could extend later
        # by reading the file at PreToolUse time.
        return None
    if not my_old:
        return None
    target_hash = _stable_string_hash(my_old)

    # `inserted_string_hashes` lives in the PostToolUse record's
    # explain.post_analysis block, so look at PostToolUse audit
    # entries (the prior record where the agent INSERTED the text we
    # are now removing).
    for rec in _walk_audit_records_reverse(audit_path, lookback):
        if rec.get("hook") != "PostToolUse":
            continue
        if rec.get("tool") not in ("Edit", "MultiEdit"):
            continue
        explain = rec.get("explain") or {}
        prior = explain.get("post_analysis") or {}
        prior_inserts = prior.get("inserted_string_hashes") or []
        prior_path = prior.get("file_path") or ""
        if prior_path != file_path:
            continue
        if target_hash in prior_inserts:
            # PostToolUse records carry the *invocation_id*; the
            # matching PreToolUse decision (with `decision` field)
            # is what `aegis report --explain` keys off, so we
            # surface that. Falls back to invocation_id if absent.
            return BacktrackEvidence(
                reverted_trace_id=str(
                    rec.get("trace_id")
                    or rec.get("invocation_id")
                    or ""
                ),
                file_path=file_path,
                matched_string_hash=target_hash,
            )
    return None


def inserted_string_hashes_for_audit(
    *, tool_name: str, tool_input: dict[str, Any],
) -> list[str]:
    """For Edit/MultiEdit, return a list of SHA3 prefixes of every
    ``new_string`` so the *next* PostToolUse can detect a revert
    against this call. Empty list otherwise."""
    out: list[str] = []
    if tool_name == "Edit":
        ns = tool_input.get("new_string") or ""
        if ns:
            out.append(_stable_string_hash(ns))
    elif tool_name == "MultiEdit":
        for e in tool_input.get("edits") or []:
            if isinstance(e, dict):
                ns = e.get("new_string") or ""
                if ns:
                    out.append(_stable_string_hash(ns))
    return out


# ─────────────────────────────────────────────────────────────────────
# 3. Redundancy detection
# ─────────────────────────────────────────────────────────────────────


def detect_redundant_call(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    audit_path: Path,
    lookback: int = DEFAULT_BACKTRACK_LOOKBACK,
) -> str | None:
    """Same tool + same canonical args within the last ``lookback``
    PreToolUse records → return the trace_id of the prior call.

    Note: step336 already detects same-call repetition for *gating*
    purposes (3+ in a row → REQUIRE_APPROVAL). This is the lighter
    forensic version that fires on any duplicate (not just bursts)
    so the Stop-hook KPI can compute a redundancy ratio."""
    args_hash = hashlib.sha3_256(
        json.dumps(tool_input, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    # args_hash is recorded in PostToolUse explain.post_analysis (set
    # by `to_audit_dict` on the previous call). Walk PostToolUse
    # entries, not PreToolUse decisions.
    for rec in _walk_audit_records_reverse(audit_path, lookback):
        if rec.get("hook") != "PostToolUse":
            continue
        if rec.get("tool") != tool_name:
            continue
        explain = rec.get("explain") or {}
        prior = explain.get("post_analysis") or {}
        prior_args_hash = prior.get("args_hash")
        if prior_args_hash == args_hash:
            return str(
                rec.get("trace_id") or rec.get("invocation_id") or ""
            ) or None
    return None


# ─────────────────────────────────────────────────────────────────────
# 4. Duration via ATMU intent log
# ─────────────────────────────────────────────────────────────────────


def compute_duration_ms(
    record_id: str, intent_log_path: Path,
) -> float | None:
    """Pre→Post wall-clock for this record_id using the ATMU intent
    log's ``created_at_ns`` column (set at TENTATIVE insert time in
    PR #31). Returns ``None`` if the record isn't found or DB is
    missing — caller should treat absence as "duration unknown"."""
    if not intent_log_path.is_file():
        return None
    import time
    try:
        conn = sqlite3.connect(str(intent_log_path), timeout=1.0)
        try:
            row = conn.execute(
                "SELECT created_at_ns FROM intent_log WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    created_ns = int(row[0])
    return (time.time_ns() - created_ns) / 1_000_000.0


# ─────────────────────────────────────────────────────────────────────
# 5. Convenience: full analysis for a PostToolUse event
# ─────────────────────────────────────────────────────────────────────


def analyse_post_tool_event(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_response: Any,
    exit_code: int | None,
    audit_path: Path,
    intent_log_path: Path,
    record_id: str | None = None,
    lookback: int = DEFAULT_BACKTRACK_LOOKBACK,
) -> PostAnalysis:
    """Run all four analyzers and bundle into a single dataclass.

    Order matters: classification first (cheap, always runs);
    backtrack/redundancy require audit walk so they're conditioned
    on the tool type. duration is a cheap SQLite point-lookup.
    """
    classification = classify_response(tool_response, exit_code=exit_code)
    backtrack = detect_backtrack(
        tool_name=tool_name, tool_input=tool_input,
        audit_path=audit_path, lookback=lookback,
    )
    redundant_of = detect_redundant_call(
        tool_name=tool_name, tool_input=tool_input,
        audit_path=audit_path, lookback=lookback,
    )
    duration_ms: float | None = None
    if record_id:
        duration_ms = compute_duration_ms(record_id, intent_log_path)
    return PostAnalysis(
        classification=classification,
        duration_ms=duration_ms,
        backtrack=backtrack,
        redundant_of=redundant_of,
    )


def to_audit_dict(
    analysis: PostAnalysis,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Flatten the dataclass into the dict that lives in the audit
    record's explain.post_analysis block. Adds the ``args_hash`` and
    ``inserted_string_hashes`` that *future* PostToolUse calls will
    walk back to detect backtrack / redundancy against this one."""
    out: dict[str, Any] = {
        "classification": asdict(analysis.classification),
        "args_hash": hashlib.sha3_256(
            json.dumps(tool_input, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16],
        "duration_ms": analysis.duration_ms,
        "redundant_of": analysis.redundant_of,
    }
    if analysis.backtrack is not None:
        out["backtrack"] = asdict(analysis.backtrack)
    inserted = inserted_string_hashes_for_audit(
        tool_name=tool_name, tool_input=tool_input,
    )
    if inserted:
        out["inserted_string_hashes"] = inserted
        out["file_path"] = tool_input.get("file_path", "")
    return out
