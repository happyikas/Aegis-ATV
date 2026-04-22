"""Judge abstract interface (PLAN 6.5 + patent ¶[0066] M13).

The patent specifies three output heads:
    verdict       — 3-class categorical (ALLOW / BLOCK / REQUIRE_APPROVAL)
    confidence    — calibrated probability scalar
    attribution   — per-subfield contribution score (30 entries) indicating
                    which ATV subfield drove the verdict. Used for
                    interpretability + regulatory audit + cost-vs-other
                    attack disambiguation (¶[0066] last sentence).

For T2 the attribution comes from the Haiku judge as JSON; the dummy
judge returns an empty dict (no real signal).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class JudgeVerdict:
    decision: Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"]
    confidence: float
    reason: str
    # Patent ¶[0066]: per-subfield contribution scores in [0, 1].
    # Keys are the subfield names from aegis.schema.ALL_SUBFIELDS.
    # Empty dict for backends (e.g. dummy) that don't compute attribution.
    subfield_attribution: dict[str, float] = field(default_factory=dict)


class Judge(ABC):
    @abstractmethod
    def evaluate(self, summary: str) -> JudgeVerdict: ...
