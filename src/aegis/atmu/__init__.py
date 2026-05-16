"""ATMU — Agent Telemetry Management Unit (patent §5A).

The Write-Ahead Intent Log + 2-PC state machine + checkpoint /
compensation primitives that bracket every tool invocation. Despite
the name "transaction" semantics in the implementation, the canonical
expansion of the acronym is **Agent Telemetry Management Unit** for
patent and documentation purposes.
"""

from aegis.atmu.checkpoint import HIGH_BLAST_THRESHOLD, make_checkpoint
from aegis.atmu.compensating import plan_for
from aegis.atmu.intent_log import IntentLog
from aegis.atmu.recovery import (
    DEFAULT_MAX_AGE_HOURS,
    NON_TERMINAL_STATES,
    OrphanRecord,
    OrphanSweepResult,
    find_orphans,
    recover_orphans,
    render_sweep_summary,
)
from aegis.atmu.state_machine import (
    TERMINAL_STATES,
    InvalidTransition,
    TxState,
    can_transition,
    ensure_transition,
)

__all__ = [
    "DEFAULT_MAX_AGE_HOURS",
    "HIGH_BLAST_THRESHOLD",
    "IntentLog",
    "InvalidTransition",
    "NON_TERMINAL_STATES",
    "OrphanRecord",
    "OrphanSweepResult",
    "TERMINAL_STATES",
    "TxState",
    "can_transition",
    "ensure_transition",
    "find_orphans",
    "make_checkpoint",
    "plan_for",
    "recover_orphans",
    "render_sweep_summary",
]
