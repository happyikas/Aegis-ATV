"""Judge abstract interface (PLAN 6.5)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


@dataclass
class JudgeVerdict:
    decision: Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"]
    confidence: float
    reason: str


class Judge(ABC):
    @abstractmethod
    def evaluate(self, summary: str) -> JudgeVerdict: ...
