"""Judge backends. ``get_judge()`` returns a Judge per current settings."""

from __future__ import annotations

from aegis.config import settings
from aegis.judge.base import Judge, JudgeVerdict
from aegis.judge.dummy import DummyJudge

__all__ = ["Judge", "JudgeVerdict", "get_judge"]


def get_judge() -> Judge:
    provider = settings.aegis_judge_provider
    if provider == "haiku":
        if not settings.anthropic_api_key:
            return DummyJudge()
        from aegis.judge.haiku import HaikuJudge

        return HaikuJudge()
    if provider == "dummy":
        return DummyJudge()
    if provider == "local-phi":
        raise NotImplementedError("local-phi judge is a stretch goal (PLAN 8)")
    raise ValueError(f"Unknown judge provider: {provider}")
