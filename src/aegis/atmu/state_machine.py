"""ATMU transaction state machine (patent APPENDIX B + Section 5A).

Seven terminal/non-terminal states:

    tentative    intent recorded before release
    prepared     policy + safety checks passed
    committed    action released (or effects preserved)
    aborted      pre-execution suppression (no side effects)
    rolled-back  checkpoint restored (side effects reversed)
    compensated  external compensating action issued
    quarantined  AID placed in circuit-breaker mode

Legal transitions are defined declaratively; arbitrary state jumps
(e.g. aborted → committed) raise InvalidTransition at the state
machine boundary so the WAL can't be corrupted even by malicious
callers.
"""

from __future__ import annotations

from enum import StrEnum


class TxState(StrEnum):
    TENTATIVE = "tentative"
    PREPARED = "prepared"
    COMMITTED = "committed"
    ABORTED = "aborted"
    ROLLED_BACK = "rolled-back"
    COMPENSATED = "compensated"
    QUARANTINED = "quarantined"


# Legal forward transitions per APPENDIX B.
_LEGAL: dict[TxState, frozenset[TxState]] = {
    TxState.TENTATIVE:   frozenset({TxState.PREPARED, TxState.ABORTED, TxState.QUARANTINED}),
    TxState.PREPARED:    frozenset({TxState.COMMITTED, TxState.ABORTED, TxState.QUARANTINED}),
    TxState.COMMITTED:   frozenset({TxState.ROLLED_BACK, TxState.COMPENSATED, TxState.QUARANTINED}),
    TxState.ABORTED:     frozenset({TxState.QUARANTINED}),          # usually terminal
    TxState.ROLLED_BACK: frozenset({TxState.QUARANTINED}),          # terminal
    TxState.COMPENSATED: frozenset({TxState.QUARANTINED}),          # terminal
    TxState.QUARANTINED: frozenset(),                                # terminal until admin release
}

TERMINAL_STATES: frozenset[TxState] = frozenset(
    {TxState.COMMITTED, TxState.ABORTED, TxState.ROLLED_BACK,
     TxState.COMPENSATED, TxState.QUARANTINED}
)


class InvalidTransitionError(ValueError):
    """Raised when a caller attempts an illegal ATMU state change."""


# Backward-compat alias (some early callers may still import the old name).
InvalidTransition = InvalidTransitionError


def can_transition(from_state: TxState, to_state: TxState) -> bool:
    return to_state in _LEGAL.get(from_state, frozenset())


def ensure_transition(from_state: TxState, to_state: TxState) -> None:
    if not can_transition(from_state, to_state):
        raise InvalidTransition(f"ATMU: {from_state.value} → {to_state.value} is not legal")
