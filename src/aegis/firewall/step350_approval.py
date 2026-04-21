"""Step 350 — Approval notification dispatch (patent ¶[0061]).

When the Action Firewall decision is REQUIRE_APPROVAL, step 350
suspends tool execution and dispatches a notification to an approver
through a configured channel. On receipt of an approval response the
tool proceeds (via /approve); on timeout or denial it is blocked.

For the T2 MVP the 'dispatch' is a structured event emitted to stderr
+ structlog; plugging in a real webhook/Slack/email channel is a
configuration concern layered on top.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from aegis.schema import ATVInput, Verdict


@dataclass
class ApprovalRequest:
    """The notification payload emitted when a call is held for a human."""

    atv_id: str
    aid: str
    tenant_id: str
    tool_name: str
    reason: str
    requested_at_ns: int
    trace: dict[str, str] = field(default_factory=dict)
    tool_args_preview: str = ""


# Pluggable channel. Tests override via ``set_channel``.
_channel_name: str = os.environ.get("AEGIS_APPROVAL_CHANNEL", "stderr")
_emitted: list[ApprovalRequest] = []


def set_channel(name: str) -> None:
    """Test-only: swap the dispatch channel."""
    global _channel_name
    _channel_name = name


def drain_emitted() -> list[ApprovalRequest]:
    """Test-only: consume the buffered dispatch records."""
    out = list(_emitted)
    _emitted.clear()
    return out


def dispatch(verdict: Verdict, inp: ATVInput) -> ApprovalRequest | None:
    """If ``verdict`` is REQUIRE_APPROVAL, build + emit a notification.

    Returns the ApprovalRequest that was emitted, or None if the verdict
    didn't need approval. No-op for ALLOW / BLOCK.
    """
    if verdict.decision != "REQUIRE_APPROVAL":
        return None

    req = ApprovalRequest(
        atv_id=verdict.atv_id,
        aid=inp.header.aid,
        tenant_id=inp.header.tenant_id,
        tool_name=inp.tool_name,
        reason=verdict.reason,
        requested_at_ns=time.time_ns(),
        trace=dict(verdict.step_traces),
        tool_args_preview=inp.tool_args_json[:200],
    )

    # Buffer for tests / audit.
    _emitted.append(req)

    # Channel dispatch.
    if _channel_name == "stderr":
        print(
            f"[aegis-approval] PENDING atv={req.atv_id} aid={req.aid} "
            f"tool={req.tool_name}  reason={req.reason}",
            file=sys.stderr,
            flush=True,
        )
    elif _channel_name == "silent":
        pass  # useful in tests that just want to inspect _emitted
    # Other channels (webhook/slack/email) are out of scope for the T2 MVP.

    return req


def dispatch_to_dict(req: ApprovalRequest) -> dict[str, Any]:
    return {
        "atv_id": req.atv_id,
        "aid": req.aid,
        "tenant_id": req.tenant_id,
        "tool_name": req.tool_name,
        "reason": req.reason,
        "requested_at_ns": req.requested_at_ns,
        "trace": req.trace,
        "tool_args_preview": req.tool_args_preview,
    }
