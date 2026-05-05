#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook — user retry detection.

Fires every time the user submits a message. We compare the
current prompt to the most recent prior user prompt in the
transcript. High similarity (Jaccard ≥ threshold, default 0.5,
or BGE cosine if PR #25 BGE provider is configured) is a signal
that the user is *retrying* — a strong indicator the previous
session work didn't actually solve the problem.

Each event yields one ``hook="UserPromptSubmit"`` audit record
carrying:

* prompt_hash (16-char SHA3 prefix — never the raw text)
* prompt_size_bytes
* similarity to previous prompt
* is_retry flag
* method ("jaccard" or "bge_cosine")
* (opt-in) preview — first 80 chars via
  AEGIS_USER_PROMPT_CAPTURE_PREVIEW=1

Failure mode
------------

NEVER blocks Claude Code (always exit 0). Errors → stderr,
swallowed. Default privacy: raw prompt never lands in audit.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import IO, Any

LOCAL_AUDIT_PATH = Path(
    os.environ.get(
        "AEGIS_LOCAL_AUDIT", str(Path.home() / ".aegis" / "audit.jsonl")
    )
)
# RETRY_THRESHOLD = None → method-aware auto-pick (PR #57): 0.85 for
# BGE-cosine, 0.5 for Jaccard. Setting AEGIS_USER_RETRY_THRESHOLD pins
# a single threshold regardless of method (use this only when you've
# tuned to a specific deployment).
_threshold_env = os.environ.get("AEGIS_USER_RETRY_THRESHOLD", "").strip()
RETRY_THRESHOLD: float | None = (
    float(_threshold_env) if _threshold_env else None
)
VERBOSE = os.environ.get("AEGIS_HOOK_VERBOSE", "0") == "1"


def _emit(msg: str) -> None:
    print(f"[aegis-prompt] {msg}", file=sys.stderr, flush=True)


def _append_audit(record: dict[str, Any]) -> None:
    try:
        from aegis.audit.local_chain import append as chain_append
        chain_append(LOCAL_AUDIT_PATH, record)
    except OSError:
        pass
    except Exception as e:  # noqa: BLE001 — never crash
        if VERBOSE:
            _emit(f"audit append failed: {e}")


def handle_user_prompt_submit(
    stdin: IO[str] | None = None, stdout: IO[str] | None = None
) -> int:
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout

    raw = in_stream.read()
    if not raw or not raw.strip():
        return 0
    try:
        event: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    if event.get("hook_event_name") not in (None, "", "UserPromptSubmit"):
        return 0

    session_id = event.get("session_id", "")
    transcript = event.get("transcript_path", "")
    prompt = event.get("prompt") or event.get("user_prompt") or ""

    summary: dict[str, Any] = {"user_retry": "skipped"}
    try:
        from aegis.cost.user_retry_detector import (
            detect_user_retry,
            to_audit_record,
        )

        evidence = detect_user_retry(
            current_prompt=str(prompt),
            transcript_path=Path(transcript) if transcript else None,
            threshold=RETRY_THRESHOLD,
        )
        _append_audit(to_audit_record(session_id, evidence))
        summary = {
            "user_retry": "recorded",
            "is_retry": evidence.is_retry,
            "similarity": evidence.similarity,
            "method": evidence.method,
            "threshold": evidence.threshold,
        }
        if VERBOSE and evidence.is_retry:
            _emit(
                f"retry detected — similarity={evidence.similarity:.2f} "
                f"({evidence.method})"
            )
    except Exception as e:  # noqa: BLE001 — never crash
        summary = {"user_retry": "error", "error": str(e)}
        if VERBOSE:
            _emit(f"error: {e}")

    print(json.dumps({"_aegis": summary}), file=out_stream)
    return 0


def main() -> int:
    return handle_user_prompt_submit()


if __name__ == "__main__":
    raise SystemExit(main())
