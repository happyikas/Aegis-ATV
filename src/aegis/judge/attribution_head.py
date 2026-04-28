"""M13 attribution-head Judge (v2.5, patent ¶[0066] + Claim 8).

Frozen linear classifier over the 30 named ATV subfields. Reads the
2080-D ATV vector directly (not a text summary), aggregates each
subfield's normalized danger signal, applies a hand-tuned weight
vector, and emits ``(decision, confidence, per-subfield attribution)``.

Why "M13" matters
-----------------
The patent reserves the third sLLM output head for **per-subfield
attribution scores** so a verdict is interpretable axis-by-axis. The
v2.0–v2.4 sLLM judges (Dummy, Haiku) only expose ``decision`` +
``reason``; they don't fill ``JudgeVerdict.subfield_attribution``.
This module is the first first-class implementation:

* **<1 ms inference** — pure float32 dot product over 30 scalars.
* **100 % bit-identical** — IEEE-754 deterministic on any CPU/GPU,
  no model weights drift, no API.
* **Auditable** — ``model_hash`` is the SHA3-256 of the frozen
  ``models/m13_attribution_head_v1.json`` file. ``aegis verify-audit``
  can re-run the classifier on a stored ATV and reproduce the exact
  verdict bit-for-bit.
* **30-key attribution dict** populated for ``aegis report -v``,
  step340 trace logging, and downstream compliance dashboards.

Backward compatibility
----------------------
``AttributionHead.evaluate(summary)`` falls back to a tiny regex over
the summary text (mirrors :class:`aegis.judge.dummy.DummyJudge`) so
callers that haven't switched to ``evaluate_full`` still get a
reasonable verdict — just without slot-level signal.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np

from aegis.judge.base import Judge, JudgeVerdict
from aegis.schema import ALL_SUBFIELDS, ATVInput

DEFAULT_WEIGHTS_PATH: Path = (
    Path(__file__).resolve().parents[3] / "models" / "m13_attribution_head_v1.json"
)


@dataclass(frozen=True)
class _FrozenWeights:
    subfield_weights: dict[str, float]
    named_slot_weights: dict[str, list[tuple[str, float]]]
    threshold_block: float
    threshold_approval: float
    model_hash: str


@lru_cache(maxsize=4)
def _load_weights(path_str: str) -> _FrozenWeights:
    """Read + canonicalise + SHA3-hash the frozen weights file."""
    p = Path(path_str)
    raw_bytes = p.read_bytes()
    model_hash = hashlib.sha3_256(raw_bytes).hexdigest()
    data: dict[str, Any] = json.loads(raw_bytes.decode("utf-8"))

    sw_raw = data.get("subfield_weights", {})
    if not isinstance(sw_raw, dict):
        raise ValueError("subfield_weights must be a dict")
    sw = {str(k): float(v) for k, v in sw_raw.items()}

    nsw_raw = data.get("named_slot_weights", {})
    nsw: dict[str, list[tuple[str, float]]] = {}
    if isinstance(nsw_raw, dict):
        for sf, slots in nsw_raw.items():
            if not isinstance(slots, list) or sf == "_comment":
                continue
            nsw[sf] = [
                (str(slot[0]), float(slot[1])) for slot in slots
                if isinstance(slot, list) and len(slot) == 2
            ]

    th_raw = data.get("thresholds", {})
    th_block = float(th_raw.get("block", 0.70))
    th_approval = float(th_raw.get("require_approval", 0.40))
    return _FrozenWeights(
        subfield_weights=sw,
        named_slot_weights=nsw,
        threshold_block=th_block,
        threshold_approval=th_approval,
        model_hash=model_hash,
    )


def reset_weights_cache() -> None:
    """Test helper — drop the cached weights so a re-edited JSON is picked up."""
    _load_weights.cache_clear()


def _aggregate_subfield(slice_arr: np.ndarray) -> float:
    """Default aggregator: max(|slice|) — picks the strongest signal in
    the subfield. Bounded to [0, 1] because all encoders clamp to that.
    """
    if slice_arr.size == 0:
        return 0.0
    return float(min(1.0, max(0.0, float(np.abs(slice_arr).max()))))


def _named_slot_score(
    slice_arr: np.ndarray, slot_weights: list[tuple[str, float]]
) -> float:
    """Sum of slot[i] × weight[i] over the slots the manifest names.
    Slots beyond the named list contribute via the default aggregator."""
    score = 0.0
    for i, (_name, weight) in enumerate(slot_weights):
        if i >= slice_arr.size:
            break
        v = float(slice_arr[i])
        score += weight * v
    return float(min(1.0, max(0.0, score)))


# Fallback regex for evaluate(summary) — mirrors DummyJudge so a caller
# that doesn't pass ATV still gets a non-trivial verdict.
_BLOCK_KEYWORDS = ("transfer", "delete", "shutdown", "drop")
_APPROVAL_TOOLS = ("transfer_funds", "execute_shell", "send_email")


class AttributionHead(Judge):
    """Frozen linear classifier — the M13 head per Claim 8."""

    def __init__(self, weights_path: Path | None = None) -> None:
        self._weights_path = weights_path or DEFAULT_WEIGHTS_PATH
        self._weights = _load_weights(str(self._weights_path))

    @property
    def model_hash(self) -> str:
        return self._weights.model_hash

    # ── Default text path (legacy callers) ───────────────────────────
    def evaluate(self, summary: str) -> JudgeVerdict:
        """Mirror DummyJudge for callers that don't pass an ATV."""
        action_text = " ".join(
            line for line in summary.lower().splitlines()
            if line.startswith(("tool:", "args:"))
        )
        for kw in _BLOCK_KEYWORDS:
            if kw in action_text:
                return JudgeVerdict(
                    "BLOCK", 0.6,
                    f"attribution-head fallback (text only): keyword '{kw}'",
                    model_hash=self.model_hash,
                )
        for tool in _APPROVAL_TOOLS:
            if f"tool: {tool}" in action_text:
                return JudgeVerdict(
                    "REQUIRE_APPROVAL", 0.6,
                    f"attribution-head fallback (text only): tool '{tool}'",
                    model_hash=self.model_hash,
                )
        return JudgeVerdict(
            "ALLOW", 0.7,
            "attribution-head fallback (text only): no red flags",
            model_hash=self.model_hash,
        )

    # ── Rich path — reads the ATV vector + ATVInput directly ────────
    def evaluate_full(
        self,
        summary: str,
        *,
        atv: np.ndarray | None = None,
        inp: ATVInput | None = None,
    ) -> JudgeVerdict:
        if atv is None:
            return self.evaluate(summary)

        t0 = time.perf_counter_ns()
        attribution: dict[str, float] = {}
        score = 0.0

        for subfield_name, slc in ALL_SUBFIELDS:
            sf_arr = atv[slc]
            base = _aggregate_subfield(sf_arr)
            if subfield_name in self._weights.named_slot_weights:
                base = max(
                    base,
                    _named_slot_score(
                        sf_arr, self._weights.named_slot_weights[subfield_name]
                    ),
                )
            weight = self._weights.subfield_weights.get(subfield_name, 0.0)
            contribution = base * weight
            attribution[subfield_name] = float(contribution)
            score += contribution

        # Score is unbounded; clamp to [0, 1] for confidence display.
        score_clamped = float(min(1.0, max(0.0, score)))
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000

        decision: Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"]
        if score >= self._weights.threshold_block:
            decision = "BLOCK"
            top_subfield = max(attribution.items(), key=lambda kv: kv[1])
            reason = (
                f"attribution-head BLOCK (score={score:.2f} ≥ "
                f"{self._weights.threshold_block:.2f}); "
                f"top contributor: {top_subfield[0]} ({top_subfield[1]:.2f})"
            )
        elif score >= self._weights.threshold_approval:
            decision = "REQUIRE_APPROVAL"
            top_subfield = max(attribution.items(), key=lambda kv: kv[1])
            reason = (
                f"attribution-head REQUIRE_APPROVAL (score={score:.2f} ≥ "
                f"{self._weights.threshold_approval:.2f}); "
                f"top contributor: {top_subfield[0]} ({top_subfield[1]:.2f})"
            )
        else:
            decision = "ALLOW"
            reason = (
                f"attribution-head ALLOW (score={score:.2f} < "
                f"{self._weights.threshold_approval:.2f})"
            )

        return JudgeVerdict(
            decision=decision,
            confidence=score_clamped,
            reason=reason,
            subfield_attribution=attribution,
            model_hash=self.model_hash,
            latency_ms=round(elapsed_ms, 3),
        )
