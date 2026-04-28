"""Step 336 — Loop & Redundant Call Saver (v2.1.3, Day-1 #6).

Sits between step335 (cost gate) and step340 (sLLM judge). Asks the
shared :class:`aegis.monitor.loop_detector.LoopDetector` whether the
current call is a repeat of something we've seen this session.

Decisions:

* **loop**       (count ≥ ``loop_threshold``) → REQUIRE_APPROVAL.
                  The call is allowed once with explicit human OK so
                  the agent isn't stuck in an infinite retry. step340
                  is skipped (the loop signal alone is enough).
* **redundant**  (read-only repeat within window) → still ALLOW, but
                  publishes ``ctx.extras["redundant"] = True`` and a
                  note in the trace so the risk report can later count
                  ``N redundant calls deduped'' (Day-1 #7).
* **none**       (fresh call) → no-op.

The detector is keyed off ``inp.header.aid`` (the agent / session id),
so cross-session bleeds are impossible.
"""

from __future__ import annotations

import numpy as np

from aegis.firewall.core import FirewallContext, StepResult
from aegis.monitor.loop_detector import get_default_detector
from aegis.schema import ATVInput


def run(
    atv: np.ndarray, inp: ATVInput, ctx: FirewallContext
) -> StepResult:
    detector = get_default_detector()
    verdict = detector.observe(
        session_id=inp.header.aid or "default",
        tool=inp.tool_name,
        args=inp.tool_args_json,
    )

    ctx.extras["loop_count"] = verdict.count
    ctx.extras["loop_args_hash"] = verdict.args_hash

    if verdict.kind == "loop":
        return StepResult(
            verdict="REQUIRE_APPROVAL",
            reason=verdict.reason,
            trace=f"step336: loop ({verdict.count}× seen) — {inp.tool_name}",
        )
    if verdict.kind == "redundant":
        ctx.extras["redundant"] = True
        return StepResult(
            verdict=None,
            reason="",
            trace=f"step336: redundant read-only ({verdict.count}× seen)",
        )
    return StepResult(
        verdict=None, reason="", trace="step336: fresh call"
    )
