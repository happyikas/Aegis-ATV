"""Deterministic stub judge.

Used when ``AEGIS_JUDGE_PROVIDER=dummy`` or when ``haiku`` is selected but no
``ANTHROPIC_API_KEY`` is configured. Lets the rest of the pipeline run
end-to-end with no external API.

Heuristic (intentionally simple):
* Tool args contain any of {"transfer", "delete", "shutdown", "drop"} → BLOCK
* tool_name in {"transfer_funds", "execute_shell"} → REQUIRE_APPROVAL
* Otherwise ALLOW.
"""

from __future__ import annotations

from aegis.judge.base import Judge, JudgeVerdict

_BLOCK_KEYWORDS = ("transfer", "delete", "shutdown", "drop")
_APPROVAL_TOOLS = ("transfer_funds", "execute_shell", "send_email")


class DummyJudge(Judge):
    def evaluate(self, summary: str) -> JudgeVerdict:
        # Restrict keyword scan to Tool/Args lines so unrelated free-text in
        # the Plan field doesn't trip the block.
        lines = summary.lower().splitlines()
        action_text = " ".join(line for line in lines if line.startswith(("tool:", "args:")))
        for kw in _BLOCK_KEYWORDS:
            if kw in action_text:
                return JudgeVerdict("BLOCK", 0.6, f"dummy judge: matched keyword '{kw}'")
        for tool in _APPROVAL_TOOLS:
            if f"tool: {tool}" in action_text:
                return JudgeVerdict(
                    "REQUIRE_APPROVAL",
                    0.6,
                    f"dummy judge: high-impact tool '{tool}'",
                )
        return JudgeVerdict("ALLOW", 0.8, "dummy judge: no red flags")
