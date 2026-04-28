"""Judge backends. ``get_judge()`` returns a Judge per current settings.

Providers (selected by ``AEGIS_JUDGE_PROVIDER``):

* ``dummy`` (default)        — regex over text summary, <1ms
* ``attribution_head``       — v2.5 frozen 30-feature linear classifier
                                (Claim 8). Bit-deterministic, <1ms.
* ``haiku``                  — Claude 3.5 Haiku via Anthropic API
                                (~150ms p50, semantically rich, "stable" det)
* ``local-phi``              — v2.6 Phi-4-mini-q4 stub (~50ms p50, attestable
                                deterministic when model file present)
* ``hybrid``                 — v3.0 confidence-routing combiner: M13 attribution
                                head ─→ local Phi ─→ cloud Haiku ─→ dummy
"""

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
    if provider == "attribution_head":
        from aegis.judge.attribution_head import AttributionHead

        return AttributionHead()
    if provider == "local-phi":  # v2.6
        from aegis.judge.local_phi import LocalPhiJudge

        return LocalPhiJudge()
    if provider == "hybrid":  # v3.0
        from aegis.judge.hybrid import HybridJudge

        return HybridJudge()
    raise ValueError(f"Unknown judge provider: {provider}")
