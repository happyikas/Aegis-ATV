"""Step 320 — Blast Radius (PLAN 6.4).

Looks up the tool in a static table and publishes the value into
``ctx.blast_radius`` for later steps.
"""

from __future__ import annotations

import numpy as np

from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

TOOL_BLAST_TABLE: dict[str, int] = {
    "read_file": 1,
    "list_directory": 1,
    "write_file": 3,
    "execute_shell": 8,
    "call_external_api": 5,
    "send_email": 6,
    "db_query": 2,
    "db_mutation": 7,
    "transfer_funds": 10,
    "delete_file": 6,
}

UNKNOWN_TOOL_BLAST = 5


def run(atv: np.ndarray, inp: ATVInput, ctx: FirewallContext) -> StepResult:
    blast = TOOL_BLAST_TABLE.get(inp.tool_name, UNKNOWN_TOOL_BLAST)
    ctx.blast_radius = blast
    return StepResult(None, "", f"step320: blast={blast} (tool={inp.tool_name})")
