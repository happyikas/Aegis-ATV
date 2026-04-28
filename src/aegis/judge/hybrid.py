"""HybridJudge — placeholder, real implementation in v3.0."""

from __future__ import annotations

from aegis.judge.base import Judge, JudgeVerdict


class HybridJudge(Judge):
    def evaluate(self, summary: str) -> JudgeVerdict:
        raise NotImplementedError(
            "HybridJudge is the v3.0 stub. Set AEGIS_JUDGE_PROVIDER=dummy or "
            "attribution_head until v3.0 lands."
        )
