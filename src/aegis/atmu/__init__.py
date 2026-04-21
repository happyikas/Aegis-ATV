"""Agent Transaction Management Unit (patent §5A)."""

from aegis.atmu.checkpoint import HIGH_BLAST_THRESHOLD, make_checkpoint
from aegis.atmu.compensating import plan_for
from aegis.atmu.intent_log import IntentLog
from aegis.atmu.state_machine import (
    TERMINAL_STATES,
    InvalidTransition,
    TxState,
    can_transition,
    ensure_transition,
)

__all__ = [
    "HIGH_BLAST_THRESHOLD",
    "IntentLog",
    "InvalidTransition",
    "TERMINAL_STATES",
    "TxState",
    "can_transition",
    "ensure_transition",
    "make_checkpoint",
    "plan_for",
]
