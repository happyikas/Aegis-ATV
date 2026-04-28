"""Judge abstract interface (PLAN 6.5 + patent ¶[0066] M13).

The patent specifies three output heads:
    verdict       — 3-class categorical (ALLOW / BLOCK / REQUIRE_APPROVAL)
    confidence    — calibrated probability scalar
    attribution   — per-subfield contribution score (30 entries) indicating
                    which ATV subfield drove the verdict. Used for
                    interpretability + regulatory audit + cost-vs-other
                    attack disambiguation (¶[0066] last sentence).

Two interfaces:
* ``evaluate(summary)`` — text-only, the historical contract that
  ``DummyJudge`` and ``HaikuJudge`` ship. Required for backward
  compatibility with all existing tests.
* ``evaluate_full(summary, *, atv, inp)`` — richer signature that the
  v2.5 ``AttributionHead``, v2.6 ``LocalPhiJudge`` and v3.0
  ``HybridJudge`` use to read the 30 named ATV subfields directly.
  The default implementation falls back to ``evaluate(summary)`` so
  every existing Judge keeps working unchanged.

For T2 the attribution comes from the Haiku judge as JSON; the dummy
judge returns an empty dict (no real signal). v2.5+ AttributionHead
populates the full 30-key map from frozen weights.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class JudgeVerdict:
    decision: Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"]
    confidence: float
    reason: str
    # Patent ¶[0066]: per-subfield contribution scores in [0, 1].
    # Keys are the subfield names from aegis.schema.ALL_SUBFIELDS.
    # Empty dict for backends (e.g. dummy) that don't compute attribution.
    subfield_attribution: dict[str, float] = field(default_factory=dict)
    # v2.5+ richer surface — which model decided this verdict, latency
    # observed for that decision, and (v3.0 hybrid) the per-layer
    # traces of the routing tower. Empty / None for legacy judges.
    model_hash: str | None = None
    latency_ms: float | None = None
    layer_traces: list[str] = field(default_factory=list)


class Judge(ABC):
    @abstractmethod
    def evaluate(self, summary: str) -> JudgeVerdict: ...

    def evaluate_full(
        self,
        summary: str,
        *,
        atv: Any = None,
        inp: Any = None,
    ) -> JudgeVerdict:
        """Richer entrypoint — defaults to ``evaluate(summary)``.

        ``atv`` is an :class:`numpy.ndarray` (the 2080-D ATV vector);
        ``inp`` is an :class:`aegis.schema.ATVInput`. Both are typed
        ``Any`` to avoid making ``aegis.judge.base`` depend on numpy /
        the schema module — Subclasses that need them re-import.

        Subclasses that read the 30 ATV subfields directly (M13
        attribution head, hybrid combiner) override this. step340_policy
        prefers ``evaluate_full`` and passes the live ATV vector + the
        original :class:`aegis.schema.ATVInput`.
        """
        return self.evaluate(summary)
