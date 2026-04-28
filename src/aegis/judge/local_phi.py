"""LocalPhiJudge — placeholder, real implementation in v2.6."""

from __future__ import annotations

from aegis.judge.base import Judge, JudgeVerdict


class LocalPhiJudge(Judge):
    def evaluate(self, summary: str) -> JudgeVerdict:
        raise NotImplementedError(
            "LocalPhiJudge is the v2.6 stub. Set AEGIS_JUDGE_PROVIDER=dummy or "
            "attribution_head until v2.6 lands."
        )
