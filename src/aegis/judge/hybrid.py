"""HybridJudge — confidence-routing combiner (v3.0).

Stacks three sLLM tiers in increasing (latency × cost × non-determinism)
order and routes each verdict to the *cheapest tier confident enough*
to decide. This is the architecture the M13 + Phi + Haiku conversation
landed on:

  Tier 1: M13 AttributionHead    <1 ms,  100% bit-deterministic
  Tier 2: LocalPhiJudge stub/real ~50 ms (real), <1ms (stub) — attestable
  Tier 3: HaikuJudge (cloud)      ~150 ms, "approximately stable"
  Tier 4: DummyJudge (always)     <1 ms,   100% deterministic regex

Routing
-------
A tier "decides" the call when:

* its decision is BLOCK or REQUIRE_APPROVAL, OR
* its decision is ALLOW AND ``confidence ≥ allow_threshold``.

If a tier returns ALLOW with low confidence (< allow_threshold), the
combiner *escalates* — it doesn't trust a low-confidence ALLOW. This
is the "fail-safe escalation" pattern: better to ask the next tier
than to silently let through.

Layer-traces
------------
The returned ``JudgeVerdict.layer_traces`` is a list of strings, one
per tier consulted, recording ``"tier_id: decision conf=X.XX"``. The
combiner's ``model_hash`` is set to the *deciding* tier's hash, so
``aegis verify-audit`` can re-run the exact path. ``latency_ms`` is
the cumulative wall-clock of all tiers consulted.

Provider configuration
----------------------
``AEGIS_JUDGE_PROVIDER=hybrid`` activates the full default stack
(M13 → Phi → Haiku → Dummy). Tiers are skipped when not viable:

* LocalPhi auto-skips itself (returns confidence=0.0 ALLOW) when the
  GGUF isn't present, so the combiner naturally moves on.
* Haiku is only added to the stack when ``ANTHROPIC_API_KEY`` is set;
  otherwise the local stack handles everything and Dummy is the final
  fallback.
* Dummy is always the final fallback.

Custom stacks can be built with ``HybridJudge(layers=[...])``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from aegis.judge.attribution_head import AttributionHead
from aegis.judge.base import Judge, JudgeVerdict
from aegis.judge.dummy import DummyJudge


@dataclass(frozen=True)
class HybridLayer:
    """One tier in the confidence-routing stack."""

    name: str
    judge: Judge
    allow_threshold: float = 0.50
    """Minimum confidence to *trust* an ALLOW verdict from this tier.
    Below this, the combiner escalates to the next tier."""


def _default_layers() -> list[HybridLayer]:
    """The canonical M13 → Phi → Haiku → Dummy stack."""
    from aegis.config import settings
    from aegis.judge.local_phi import LocalPhiJudge

    layers: list[HybridLayer] = [
        HybridLayer(
            name="m13_attribution",
            judge=AttributionHead(),
            allow_threshold=0.30,
        ),
        HybridLayer(
            name="local_phi", judge=LocalPhiJudge(), allow_threshold=0.40
        ),
    ]
    # Haiku tier: only include if the user has configured an Anthropic
    # key. Otherwise the local stack handles everything and there's no
    # point round-tripping to a fallback that can't authenticate.
    if settings.anthropic_api_key:
        from aegis.judge.haiku import HaikuJudge

        layers.append(
            HybridLayer(
                name="haiku", judge=HaikuJudge(), allow_threshold=0.50
            )
        )
    layers.append(
        HybridLayer(name="dummy", judge=DummyJudge(), allow_threshold=0.0)
    )
    return layers


def _is_decided(verdict: JudgeVerdict, threshold: float) -> bool:
    """True iff this tier wants to commit to its verdict.

    BLOCK / REQUIRE_APPROVAL always commit (we never escalate a
    not-allow). ALLOW commits only with sufficient confidence —
    a low-confidence ALLOW is the signal to ask the next tier.
    """
    if verdict.decision in {"BLOCK", "REQUIRE_APPROVAL"}:
        return True
    return verdict.confidence >= threshold


class HybridJudge(Judge):
    """v3.0 — confidence-routing combiner over a layered Judge stack."""

    def __init__(self, layers: list[HybridLayer] | None = None) -> None:
        self._layers = layers if layers is not None else _default_layers()
        if not self._layers:
            raise ValueError("HybridJudge requires at least one layer")

    @property
    def layers(self) -> list[HybridLayer]:
        return list(self._layers)

    def evaluate(self, summary: str) -> JudgeVerdict:
        return self.evaluate_full(summary, atv=None, inp=None)

    def evaluate_full(
        self, summary: str, *, atv: Any = None, inp: Any = None
    ) -> JudgeVerdict:
        traces: list[str] = []
        cumulative_ms = 0.0
        last: JudgeVerdict | None = None
        deciding: HybridLayer | None = None

        for layer in self._layers:
            t0 = time.perf_counter_ns()
            v = layer.judge.evaluate_full(summary, atv=atv, inp=inp)
            dt_ms = (time.perf_counter_ns() - t0) / 1_000_000
            cumulative_ms += dt_ms
            traces.append(
                f"{layer.name}: {v.decision} conf={v.confidence:.2f} "
                f"({dt_ms:.1f}ms)"
            )
            last = v
            if _is_decided(v, layer.allow_threshold):
                deciding = layer
                break

        # _layers is non-empty per __init__; loop ran at least once.
        assert last is not None
        if deciding is None:
            # Fell through every layer with low-confidence ALLOWs only.
            # The last layer wins by default (Dummy is always last).
            deciding = self._layers[-1]

        return JudgeVerdict(
            decision=last.decision,
            confidence=last.confidence,
            reason=f"hybrid[{deciding.name}]: {last.reason}",
            subfield_attribution=last.subfield_attribution,
            model_hash=last.model_hash,
            latency_ms=round(cumulative_ms, 3),
            layer_traces=traces,
        )
