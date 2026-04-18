"""Shared helpers for firewall step tests."""

from __future__ import annotations

import time

import numpy as np

from aegis.schema import ATVHeader, ATVInput, CostEfficiency

ZERO_ATV = np.zeros(2080, dtype=np.float32)


def make_input(
    *,
    tool_name: str = "read_file",
    tool_args_json: str = '{"path":"./data/x.txt"}',
    tenant_id: str = "demo-tenant",
    safety_flags: dict[str, float] | None = None,
    cost: CostEfficiency | None = None,
) -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t",
            span_id="s",
            tenant_id=tenant_id,
            aid="agent-x",
            timestamp_ns=time.time_ns(),
        ),
        agent_state_text="state",
        plan_text="plan",
        tool_name=tool_name,
        tool_args_json=tool_args_json,
        safety_flags=safety_flags or {},
        cost_estimate=cost or CostEfficiency(),
    )
