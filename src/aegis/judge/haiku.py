"""sLLM judge backed by Claude Haiku 4.5 (PLAN 6.5 + patent ¶[0066] M13)."""

from __future__ import annotations

import json
from typing import cast

from anthropic import Anthropic

from aegis.config import settings
from aegis.judge.base import Judge, JudgeVerdict

# 30 ATV subfield names — Haiku is asked to emit a contribution score for each.
_SUBFIELDS_LIST = (
    "agent_state_embedding, action_history, inter_agent_graph, "
    "memory_provenance, qom_scores, resource_access_pattern, "
    "prompt_structure, aid_ats_scalars, encryption_metadata, "
    "output_content_fingerprint, tool_arg_inspection, action_blast_radius, "
    "output_channel_diversity, session_behavioral_drift, mcp_trust_signals, "
    "grounding_metrics, novelty_score, human_oversight_state, "
    "cost_efficiency_metrics, memory_timing_histograms, aid_tag_transitions, "
    "atmu_anomaly, dma_fanout, thermal_ecc_drift, watchdog_signals, "
    "network_telemetry, gpu_accelerator_state, hypervisor_signals, "
    "hw_cost_attestation, linkage_consistency_features"
)

SYSTEM_PROMPT = f"""You are a deterministic 3-class security classifier for AI agent tool calls.
Respond with ONLY a JSON object, no prose:
{{
  "decision": "ALLOW|BLOCK|REQUIRE_APPROVAL",
  "confidence": 0.0-1.0,
  "reason": "...",
  "attribution": {{ "<subfield_name>": 0.0-1.0, ... }}
}}

Decide BLOCK if: clear malicious intent, data exfiltration, privilege escalation,
  destructive command against critical paths, clear policy violation.
Decide REQUIRE_APPROVAL if: ambiguous intent, unusual pattern, high-impact operation
  without sufficient context, or cost/scope beyond typical.
Decide ALLOW if: routine safe operation, well-within-scope, no red flags.

The "attribution" field MUST be a JSON object mapping ATV subfield names to
contribution scores in [0.0, 1.0]. The score reflects how much each subfield
influenced your verdict. Sum need not equal 1; multiple subfields can be high.
Include ONLY the subfields you actually relied on (omit zeros). Allowed
subfield names: {_SUBFIELDS_LIST}."""


class HaikuJudge(Judge):
    def __init__(self) -> None:
        self.client = Anthropic()
        self.model = "claude-haiku-4-5-20251001"
        self.temperature = settings.aegis_judge_temperature

    def evaluate(self, summary: str) -> JudgeVerdict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=400,            # bumped from 200 for the attribution dict
            temperature=self.temperature,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": summary}],
        )
        block = resp.content[0]
        text = cast(str, getattr(block, "text", "")).strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start < 0 or end <= start:
            return JudgeVerdict(
                decision="REQUIRE_APPROVAL",
                confidence=0.0,
                reason=f"unparseable judge response: {text[:80]!r}",
            )
        try:
            data = json.loads(text[start:end])
        except json.JSONDecodeError as e:
            return JudgeVerdict(
                decision="REQUIRE_APPROVAL",
                confidence=0.0,
                reason=f"invalid JSON from judge: {e}",
            )

        decision = data.get("decision", "REQUIRE_APPROVAL")
        if decision not in ("ALLOW", "BLOCK", "REQUIRE_APPROVAL"):
            decision = "REQUIRE_APPROVAL"

        # Patent ¶[0066] attribution head — clip to known subfields, [0,1].
        attribution_raw = data.get("attribution") or {}
        attribution: dict[str, float] = {}
        if isinstance(attribution_raw, dict):
            for name, value in attribution_raw.items():
                if isinstance(value, (int, float)):
                    attribution[str(name)] = max(0.0, min(1.0, float(value)))

        return JudgeVerdict(
            decision=decision,
            confidence=float(data.get("confidence", 0.5)),
            reason=str(data.get("reason", "")),
            subfield_attribution=attribution,
        )
