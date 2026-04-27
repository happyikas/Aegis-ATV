"""Cost attestation package — patent §12 + Claims 3, 26, 27, 30, 34."""

from aegis.cost.divergence import (
    HBM_BYTES_PER_TOKEN,
    DivergenceMetrics,
    compute_divergence,
    divergence_active,
    dollar_cost_divergence,
    memory_cost_divergence,
    token_to_flops_divergence,
)
from aegis.cost.escalation import (
    DEFAULT_BASELINE,
    ESCALATION_MULTIPLIER,
    EscalationDecision,
    evaluate_escalation,
)
from aegis.cost.ledger import CostAttestationLedger
from aegis.cost.model_flops import (
    DEFAULT_DOLLAR_PER_FLOP,
    FLOPS_PER_TOKEN,
    expected_dollars,
    expected_flops,
)
from aegis.cost.transcript import import_into_wal, parse_transcript

__all__ = [
    "CostAttestationLedger",
    "DEFAULT_BASELINE",
    "DEFAULT_DOLLAR_PER_FLOP",
    "DivergenceMetrics",
    "ESCALATION_MULTIPLIER",
    "EscalationDecision",
    "FLOPS_PER_TOKEN",
    "HBM_BYTES_PER_TOKEN",
    "compute_divergence",
    "divergence_active",
    "dollar_cost_divergence",
    "evaluate_escalation",
    "expected_dollars",
    "expected_flops",
    "import_into_wal",
    "memory_cost_divergence",
    "parse_transcript",
    "token_to_flops_divergence",
]
