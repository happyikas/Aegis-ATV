"""Burn-in package — patent §7 statistical baseline + 4-phase graduation."""

from aegis.burnin.controller import (
    LAYER_EXPECTED_SAMPLES,
    LAYER_WEIGHTS,
    BurnInController,
    LayerKey,
    LayerSlot,
)
from aegis.burnin.phases import (
    PRODUCTION_OVERRIDE_MAX,
    SHADOW_FPR_MAX,
    SHADOW_MIN_SAMPLES,
    SHADOW_PRECISION_MIN,
    SHADOW_TPR_MIN,
    Phase,
    PhaseMetrics,
    PhaseState,
    can_graduate,
    next_phase,
)

__all__ = [
    "BurnInController",
    "LAYER_EXPECTED_SAMPLES",
    "LAYER_WEIGHTS",
    "LayerKey",
    "LayerSlot",
    "PRODUCTION_OVERRIDE_MAX",
    "Phase",
    "PhaseMetrics",
    "PhaseState",
    "SHADOW_FPR_MAX",
    "SHADOW_MIN_SAMPLES",
    "SHADOW_PRECISION_MIN",
    "SHADOW_TPR_MIN",
    "can_graduate",
    "next_phase",
]
