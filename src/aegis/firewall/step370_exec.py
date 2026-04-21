"""Step 370 — Execution gate (patent ¶[0063]).

If the decision is ALLOW, the host may execute the tool.
If BLOCK, the tool is suppressed and an error is returned to the agent.
If REQUIRE_APPROVAL, execution is deferred until /approve returns.

The firewall does NOT actually execute the tool — it's a gate, not an
executor. This module's job is to emit a structured 'execution
recommendation' the host reads to decide what to do next. In our REST
server the recommendation is the ``decision`` field itself, already
wired through; this module annotates the verdict's step_traces so
downstream logs make the 370 boundary explicit.
"""

from __future__ import annotations

from aegis.schema import Verdict


def annotate(verdict: Verdict) -> Verdict:
    """Add a step370 trace entry to the verdict."""
    key = "aegis.firewall.step370_exec.annotate"
    if verdict.decision == "ALLOW":
        verdict.step_traces[key] = "step370: exec-recommendation=PROCEED"
    elif verdict.decision == "BLOCK":
        verdict.step_traces[key] = "step370: exec-recommendation=SUPPRESS + error-to-agent"
    else:  # REQUIRE_APPROVAL
        verdict.step_traces[key] = "step370: exec-recommendation=DEFER-until-approve"
    return verdict
