"""Compensating-action plans (patent ¶[0063H-2]).

For tools whose external side effects cannot be reversed by checkpoint
restoration (outbound payments, sent emails, irreversible API calls),
the ATMU pre-records a compensation plan at intent time. If a
post-release verdict flips to policy violation, the plan is executed
or queued.
"""

from __future__ import annotations

from typing import Any

# Tool → compensation strategy. T2 ships static defaults; T3 / future
# admins may override per tenant via policy.
DEFAULT_COMPENSATION_STRATEGIES: dict[str, dict[str, Any]] = {
    "transfer_funds": {
        "strategy": "counter_transfer",
        "params": {"reverse_amount": True},
        "human_required": True,
    },
    "send_email": {
        "strategy": "notify_recipient",
        "params": {"template": "retraction_notice"},
        "human_required": True,
    },
    "call_external_api": {
        "strategy": "cancel_request",
        "params": {"endpoint_hint": "DELETE same-resource"},
        "human_required": False,
    },
    "delete_file": {
        "strategy": "restore_from_backup",
        "params": {"backup_window_hours": 24},
        "human_required": False,
    },
    "execute_shell": {
        "strategy": "manual_remediation",
        "params": {},
        "human_required": True,
    },
}


def plan_for(tool_name: str) -> dict[str, Any] | None:
    """Return a compensation plan for ``tool_name``, or None if the
    tool doesn't need one (e.g. read_file is naturally idempotent)."""
    return DEFAULT_COMPENSATION_STRATEGIES.get(tool_name)
