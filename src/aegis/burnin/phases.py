"""4-phase Burn-in graduation (patent ¶[0075] + Claim 13).

Observation → Shadow → Assisted → Production. Transitions are gated by
predetermined numerical criteria; in production these would feed off
labelled outcomes (ground-truth red-team verdicts, post-incident
adjudications). T2 MVP exposes the gate machinery + thresholds so the
shape is right; actual TPR/FPR/precision values are stubbed at 0
until labelled data flows in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Phase(StrEnum):
    OBSERVATION = "observation"   # passive: no decisions affect execution
    SHADOW = "shadow"             # decisions produced but not enforced
    ASSISTED = "assisted"         # decisions escalate to humans
    PRODUCTION = "production"     # decisions autonomously gate actions


# Patent ¶[0075] graduation thresholds.
SHADOW_MIN_SAMPLES = 1000               # Observation → Shadow
SHADOW_TPR_MIN = 0.95                   # Shadow → Assisted
SHADOW_FPR_MAX = 0.02
SHADOW_PRECISION_MIN = 0.90
PRODUCTION_OVERRIDE_MAX = 0.05          # Assisted → Production


@dataclass
class PhaseMetrics:
    samples: int = 0
    true_positives: int = 0
    true_negatives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    human_overrides: int = 0
    human_total_decisions: int = 0
    # Gap C (#146) — verdict counters for live provider-divergence
    # detection at the L5 (per-aid × provider) layer. Populated by
    # ``BurnInController.observe`` *without* needing ground-truth
    # labels (which is why these are separate from
    # true_positives/false_positives above). Default-zero is
    # backward-compatible with serialized records from before Gap C.
    block_count: int = 0
    decision_count: int = 0

    @property
    def tpr(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @property
    def fpr(self) -> float:
        denom = self.false_positives + self.true_negatives
        return self.false_positives / denom if denom else 0.0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def override_rate(self) -> float:
        return (
            self.human_overrides / self.human_total_decisions
            if self.human_total_decisions
            else 0.0
        )

    @property
    def block_rate(self) -> float:
        """Fraction of observed verdicts that were BLOCK.

        Gap C (#146): used by the live provider-divergence advisor
        to compare a per-(aid, provider) baseline against its peers
        sharing the same aid. Zero when no decisions have been
        observed yet.
        """
        return (
            self.block_count / self.decision_count
            if self.decision_count
            else 0.0
        )


@dataclass
class PhaseState:
    current: Phase = Phase.OBSERVATION
    metrics: PhaseMetrics = field(default_factory=PhaseMetrics)
    # Free-form reasons / notes attached to each transition.
    transitions: list[dict[str, object]] = field(default_factory=list)


def can_graduate(state: PhaseState) -> tuple[bool, str]:
    """Return (eligible_for_next_phase, reason_string)."""
    m = state.metrics
    if state.current == Phase.OBSERVATION:
        if m.samples >= SHADOW_MIN_SAMPLES:
            return True, f"observation→shadow: {m.samples} ≥ {SHADOW_MIN_SAMPLES} samples"
        return False, f"need {SHADOW_MIN_SAMPLES - m.samples} more samples"
    if state.current == Phase.SHADOW:
        gates = [
            (m.tpr >= SHADOW_TPR_MIN,            f"TPR {m.tpr:.3f} ≥ {SHADOW_TPR_MIN}"),
            (m.fpr <= SHADOW_FPR_MAX,            f"FPR {m.fpr:.3f} ≤ {SHADOW_FPR_MAX}"),
            (m.precision >= SHADOW_PRECISION_MIN, f"precision {m.precision:.3f} ≥ {SHADOW_PRECISION_MIN}"),
        ]
        if all(g for g, _ in gates):
            return True, "shadow→assisted: " + " ; ".join(r for _, r in gates)
        failures = [r for g, r in gates if not g]
        return False, "shadow→assisted blocked: " + " ; ".join(failures)
    if state.current == Phase.ASSISTED:
        if m.override_rate <= PRODUCTION_OVERRIDE_MAX and m.human_total_decisions >= 100:
            return True, f"assisted→production: override {m.override_rate:.3f} ≤ {PRODUCTION_OVERRIDE_MAX}"
        return False, (
            f"assisted→production blocked: override {m.override_rate:.3f} > "
            f"{PRODUCTION_OVERRIDE_MAX} or sample {m.human_total_decisions} < 100"
        )
    return False, "production: terminal"


def next_phase(p: Phase) -> Phase:
    return {
        Phase.OBSERVATION: Phase.SHADOW,
        Phase.SHADOW: Phase.ASSISTED,
        Phase.ASSISTED: Phase.PRODUCTION,
        Phase.PRODUCTION: Phase.PRODUCTION,
    }[p]
