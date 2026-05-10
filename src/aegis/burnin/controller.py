"""Burn-in controller — 5 layers + 4 phases (patent §7 + Claims 4, 13, 14, 19, 20).

Layers (¶[0070]-[0074]):

    L1 hardware-invariant   — refresh on firmware-upgrade events       (years)
    L2 tenant               — refresh on tenant-onboarding             (quarters)
    L3 topology             — refresh on agent-composition change      (weeks)
    L4 agent-role           — refresh on new/upgraded role             (days)
    L5 instance             — continuous online micro-adjustment

Each layer carries its own PhaseState (Observation → Shadow → Assisted →
Production). The composite anomaly score is a weighted sum of per-layer
sub-scores. T2 MVP's per-layer "score" is a count-based proxy
(saturation against the layer's expected sample count); T3 will compute
real per-subfield z-scores from stored ATV vectors.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from aegis.burnin.phases import (
    Phase,
    PhaseState,
    can_graduate,
    next_phase,
)
from aegis.schema import ATVInput, Verdict

# Per-layer expected-sample budgets that drive the count-based score.
LAYER_EXPECTED_SAMPLES: dict[str, int] = {
    "L1": 1_000,
    "L2": 5_000,
    "L3": 2_000,
    "L4": 1_000,
    "L5": 500,
}

# Composite-score weights per ¶[0076] (the patent leaves them tunable;
# defaults give equal weight).
LAYER_WEIGHTS: dict[str, float] = {
    "L1": 0.20, "L2": 0.20, "L3": 0.20, "L4": 0.20, "L5": 0.20,
}


@dataclass
class LayerKey:
    """Identifies an independent baseline. Layer + scope tuple.

    Gap C (#146): the L5 layer additionally splits by ``provider``,
    so an agent that switches between LLM providers (e.g. local-llama
    vs anthropic-claude) accumulates a separate baseline per provider.
    Older serialized records had no provider field — they continue to
    deserialize cleanly because :meth:`as_str` only emits the
    ``prov=<name>`` suffix when ``provider`` is set, so old key strings
    (without the suffix) and new key strings (with it) coexist as
    distinct slots in the controller's dict.
    """
    layer: str                  # 'L1'..'L5'
    tenant_id: str | None = None
    role_id: str | None = None
    aid: str | None = None
    provider: str | None = None

    def as_str(self) -> str:
        bits = [self.layer]
        for k in (self.tenant_id, self.role_id, self.aid):
            if k:
                bits.append(k)
        if self.provider:
            # Tagged so the suffix can't collide with an aid that
            # happens to look like a provider name. Old records
            # without provider have no `prov=` segment, so they remain
            # valid keys and don't get crossed-up with new ones.
            bits.append(f"prov={self.provider}")
        return ":".join(bits)


@dataclass
class LayerSlot:
    key: LayerKey
    state: PhaseState = field(default_factory=PhaseState)
    last_observed_ns: int = 0


def _layer_keys_for(inp: ATVInput) -> list[LayerKey]:
    """Map one ATVInput to the layer slots it bumps.

    Gap C (#146): L5 additionally keys by ``inp.header.provider`` when
    the provider field is populated (PR-D / PR #136 surfaced it on the
    header). Records without a provider (the Claude Code track today,
    and any pre-#136 audit log) bucket under the old 4-part key so
    backward compat is preserved.
    """
    role = (inp.role_id or "default-role")
    tenant = inp.header.tenant_id
    aid = inp.header.aid
    provider = getattr(inp.header, "provider", None) or None
    return [
        LayerKey("L1"),                                            # global hardware baseline
        LayerKey("L2", tenant_id=tenant),                          # per-tenant
        LayerKey("L3", tenant_id=tenant),                          # topology — keyed by tenant for now
        LayerKey("L4", tenant_id=tenant, role_id=role),            # per-role
        LayerKey(
            "L5",
            tenant_id=tenant, role_id=role, aid=aid,
            provider=provider,
        ),                                                          # per-instance × provider
    ]


class BurnInController:
    """Thread-safe in-process controller. One instance per app."""

    def __init__(self) -> None:
        self._slots: dict[str, LayerSlot] = {}
        self._lock = threading.Lock()

    # ---------- observation ----------
    def observe(self, inp: ATVInput, verdict: Verdict) -> None:
        """Bump per-layer sample counts for this evaluation. T2 MVP only
        increments counts; T3 will additionally accumulate per-subfield
        sufficient statistics.

        Gap C (#146): also bumps ``decision_count`` and (when verdict
        was BLOCK) ``block_count`` so :meth:`provider_drift_for_aid`
        can compare per-(aid × provider) BLOCK rates against peer
        providers without needing ground-truth labels.
        """
        now = time.time_ns()
        is_block = verdict.decision == "BLOCK"
        with self._lock:
            for key in _layer_keys_for(inp):
                slot = self._slots.setdefault(key.as_str(), LayerSlot(key=key))
                slot.state.metrics.samples += 1
                slot.state.metrics.decision_count += 1
                if is_block:
                    slot.state.metrics.block_count += 1
                slot.last_observed_ns = now

    def record_label(
        self,
        inp: ATVInput,
        verdict: Verdict,
        *,
        ground_truth: str,
        was_human_override: bool = False,
    ) -> None:
        """Update TP/FP/TN/FN counters from a ground-truth label.

        ``ground_truth`` ∈ {"benign", "malicious"}; ``verdict.decision`` is
        compared. ALLOW vs malicious → FN; BLOCK or APPROVAL vs benign → FP.
        """
        gt_pos = ground_truth == "malicious"
        pred_pos = verdict.decision in ("BLOCK", "REQUIRE_APPROVAL")

        with self._lock:
            for key in _layer_keys_for(inp):
                slot = self._slots.setdefault(key.as_str(), LayerSlot(key=key))
                m = slot.state.metrics
                if pred_pos and gt_pos:
                    m.true_positives += 1
                elif pred_pos and not gt_pos:
                    m.false_positives += 1
                elif not pred_pos and gt_pos:
                    m.false_negatives += 1
                else:
                    m.true_negatives += 1
                if was_human_override:
                    m.human_overrides += 1
                m.human_total_decisions += 1

    # ---------- graduation ----------
    def try_graduate(self, key_str: str) -> tuple[bool, str]:
        """Attempt to advance one layer slot to the next phase."""
        with self._lock:
            slot = self._slots.get(key_str)
            if slot is None:
                return False, f"unknown layer slot: {key_str}"
            ok, reason = can_graduate(slot.state)
            if not ok:
                return False, reason
            old = slot.state.current
            slot.state.current = next_phase(old)
            slot.state.transitions.append({
                "from": old.value, "to": slot.state.current.value,
                "ts_ns": time.time_ns(), "reason": reason,
            })
            return True, reason

    # ---------- recalibration events (¶[0076]) ----------
    def event_new_role(self, tenant_id: str, role_id: str) -> None:
        """Warm-start: a new instance of an existing role inherits L4."""
        with self._lock:
            self._slots.setdefault(
                LayerKey("L4", tenant_id=tenant_id, role_id=role_id).as_str(),
                LayerSlot(key=LayerKey("L4", tenant_id=tenant_id, role_id=role_id)),
            )

    def event_topology_change(self, tenant_id: str) -> None:
        """Reset L3 baseline for a tenant — new agent composition."""
        with self._lock:
            ks = LayerKey("L3", tenant_id=tenant_id).as_str()
            self._slots[ks] = LayerSlot(key=LayerKey("L3", tenant_id=tenant_id))

    def event_tenant_onboarded(self, tenant_id: str) -> None:
        with self._lock:
            for layer in ("L2", "L3"):
                ks = LayerKey(layer, tenant_id=tenant_id).as_str()
                self._slots.setdefault(
                    ks, LayerSlot(key=LayerKey(layer, tenant_id=tenant_id))
                )

    def event_firmware_upgrade(self) -> None:
        with self._lock:
            self._slots["L1"] = LayerSlot(key=LayerKey("L1"))

    # ---------- composite anomaly score ----------
    def composite_score(self, inp: ATVInput) -> float:
        """Weighted sum of per-layer 'maturity' scores in [0, 1].

        T2 MVP: each layer's sub-score = saturation of its sample count
        against LAYER_EXPECTED_SAMPLES. A layer in OBSERVATION
        contributes 0; a fully-calibrated layer in PRODUCTION
        contributes 1. This is intentionally conservative — it tells
        operators 'how trustworthy is this verdict given current
        baseline maturity' rather than the actual anomaly likelihood.
        """
        score = 0.0
        with self._lock:
            for key in _layer_keys_for(inp):
                slot = self._slots.get(key.as_str())
                w = LAYER_WEIGHTS.get(key.layer, 0.0)
                if slot is None or slot.state.current == Phase.OBSERVATION:
                    continue
                sat = min(1.0, slot.state.metrics.samples
                          / LAYER_EXPECTED_SAMPLES.get(key.layer, 1))
                phase_factor = {
                    Phase.OBSERVATION: 0.0,
                    Phase.SHADOW: 0.4,
                    Phase.ASSISTED: 0.7,
                    Phase.PRODUCTION: 1.0,
                }[slot.state.current]
                score += w * sat * phase_factor
        return min(1.0, score)

    # ---------- Gap C (#146) — per-(aid × provider) drift -----------
    def provider_drift_for_aid(
        self,
        tenant_id: str | None,
        role_id: str | None,
        aid: str | None,
        *,
        min_samples: int = 5,
        divergence_multiplier: float = 3.0,
    ) -> list[dict[str, Any]]:
        """Compare BLOCK rates across the providers seen for one aid.

        Returns a list of dicts, one per detected divergence pair, in
        the form::

          {"aid": ..., "max_provider": ..., "max_rate": 0.50,
           "min_provider": ..., "min_rate": 0.05, "ratio": 10.0}

        Empty list when:

        * the aid has fewer than 2 L5 slots with a real provider, or
        * any candidate provider has fewer than ``min_samples``
          decisions, or
        * the BLOCK-rate ratio is below ``divergence_multiplier``.

        The ``(no-provider)`` bucket — older audit records without
        ``inp.header.provider`` — is excluded from the comparison so
        a Claude Code track aid that's been around longer doesn't
        falsely tip the drift detector. This mirrors the
        ``--by-aid-and-provider`` report-side advisor.

        Read-only; safe to call from the firewall hot path.
        """
        with self._lock:
            candidates: list[tuple[str, float, int]] = []
            for slot in self._slots.values():
                k = slot.key
                if k.layer != "L5":
                    continue
                if k.tenant_id != tenant_id or k.aid != aid:
                    continue
                if role_id is not None and k.role_id != role_id:
                    continue
                if not k.provider:
                    # (no-provider) bucket excluded — see docstring.
                    continue
                m = slot.state.metrics
                if m.decision_count < min_samples:
                    continue
                candidates.append((k.provider, m.block_rate, m.decision_count))

        if len(candidates) < 2:
            return []

        # Pairwise: compare the highest BLOCK rate against the lowest
        # non-zero (or against zero with a peer-blocking outlier flag).
        max_p, max_r, _ = max(candidates, key=lambda t: t[1])
        min_p, min_r, _ = min(candidates, key=lambda t: t[1])

        if max_r == 0:
            return []  # everyone at 0% — nothing to flag

        if min_r == 0 and max_r > 0:
            # Peer providers BLOCK; one provider doesn't. The zero-block
            # one is the suspicious side.
            return [{
                "aid": aid,
                "max_provider": max_p,
                "max_rate": max_r,
                "min_provider": min_p,
                "min_rate": 0.0,
                "ratio": float("inf"),
                "kind": "zero-block-outlier",
            }]

        ratio = max_r / min_r
        if ratio < divergence_multiplier:
            return []
        return [{
            "aid": aid,
            "max_provider": max_p,
            "max_rate": max_r,
            "min_provider": min_p,
            "min_rate": min_r,
            "ratio": ratio,
            "kind": "rate-divergence",
        }]

    def status_by_provider(self) -> dict[str, list[dict[str, Any]]]:
        """Gap C (#146) — group L5 slots by ``provider`` for a
        per-provider baseline-maturity view.

        Output shape::

          {
            "anthropic-claude-3-5": [{aid, samples, phase, ...}, ...],
            "openai-gpt-4o":        [...],
            "(no-provider)":        [...],
          }

        Provider buckets are sorted by name; entries within a bucket
        are sorted by aid. L1–L4 layers (no provider scope) are not
        included — operators view those via the unsplit
        :meth:`status` output.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        with self._lock:
            for slot in self._slots.values():
                if slot.key.layer != "L5":
                    continue
                provider_key = slot.key.provider or "(no-provider)"
                m = slot.state.metrics
                out.setdefault(provider_key, []).append({
                    "aid": slot.key.aid,
                    "tenant_id": slot.key.tenant_id,
                    "role_id": slot.key.role_id,
                    "phase": slot.state.current.value,
                    "samples": m.samples,
                    "decision_count": m.decision_count,
                    "block_count": m.block_count,
                    "block_rate": round(m.block_rate, 4),
                    "last_observed_ns": slot.last_observed_ns,
                })
        for entries in out.values():
            entries.sort(key=lambda e: (e["aid"] or "", e["tenant_id"] or ""))
        return dict(sorted(out.items()))

    # ---------- snapshot for the API ----------
    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "layers": [],
            "expected_samples": LAYER_EXPECTED_SAMPLES,
            "weights": LAYER_WEIGHTS,
        }
        with self._lock:
            for slot in sorted(self._slots.values(), key=lambda s: s.key.as_str()):
                m = slot.state.metrics
                out["layers"].append({
                    "key": slot.key.as_str(),
                    "layer": slot.key.layer,
                    "tenant_id": slot.key.tenant_id,
                    "role_id": slot.key.role_id,
                    "aid": slot.key.aid,
                    "provider": slot.key.provider,
                    "phase": slot.state.current.value,
                    "samples": m.samples,
                    "decision_count": m.decision_count,
                    "block_count": m.block_count,
                    "block_rate": round(m.block_rate, 4),
                    "tpr": round(m.tpr, 4),
                    "fpr": round(m.fpr, 4),
                    "precision": round(m.precision, 4),
                    "override_rate": round(m.override_rate, 4),
                    "last_observed_ns": slot.last_observed_ns,
                    "transitions": slot.state.transitions,
                })
        return out
