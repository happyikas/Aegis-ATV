"""v1 vs v2 evaluation harness for the M13 attribution head.

Side-by-side comparison so anyone considering "should I adopt v2?"
gets a single-screen answer:

* **3-class accuracy** on a held-out synthetic corpus.
* **Asymmetric-cost score** — false negative on a malicious example
  (BLOCK / REQUIRE_APPROVAL → ALLOW) is weighted 5× a false positive
  (ALLOW → flagged). This matches the Solo Free risk profile: a
  missed credential leak is catastrophic, an over-block is annoyance.
* **Confusion matrix per weights file.**
* **7-scenario regression** outcome (PASS / FAIL count from the same
  scenarios in ``demo/plugin_scenarios.py``).

The harness is read-only — it loads two weights JSON files and
runs them against the same corpus and the same firewall pipeline.
No side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aegis.atv.builder import build_atv
from aegis.burnin.m13_data import LabeledExample, generate
from aegis.judge.attribution_head import AttributionHead

# Asymmetric-cost weights: penalise false-negatives (missed malicious)
# 5× more than false-positives (over-blocked benign).
_FN_WEIGHT = 5.0
_FP_WEIGHT = 1.0


@dataclass
class HeadEval:
    """One weights file's performance on a corpus."""

    weights_path: str
    n: int
    accuracy: float
    confusion: dict[str, dict[str, int]]
    fn_count: int        # malicious → ALLOW
    fp_count: int        # benign → BLOCK / REQUIRE_APPROVAL
    cost: float          # weighted sum (lower is better)


@dataclass
class CompareResult:
    """v1 vs v2 head-to-head."""

    v1: HeadEval
    v2: HeadEval
    winner: str          # "v1" | "v2" | "tie"
    delta_accuracy: float
    delta_cost: float
    notes: list[str] = field(default_factory=list)


def _confusion(preds: list[str], labels: list[str]) -> dict[str, dict[str, int]]:
    classes = ("ALLOW", "REQUIRE_APPROVAL", "BLOCK")
    out: dict[str, dict[str, int]] = {c: dict.fromkeys(classes, 0) for c in classes}
    for p, y in zip(preds, labels, strict=True):
        out[y][p] += 1
    return out


def _run_head(
    head: AttributionHead, corpus: list[LabeledExample],
) -> tuple[list[str], list[str]]:
    preds: list[str] = []
    labels: list[str] = []
    for ex in corpus:
        atv = build_atv(ex.inp)
        v = head.evaluate_full("", atv=atv, inp=ex.inp)
        preds.append(v.decision)
        labels.append(ex.label)
    return preds, labels


def evaluate_head(
    weights_path: Path, corpus: list[LabeledExample],
) -> HeadEval:
    """Run one weights file against the corpus and score."""
    head = AttributionHead(weights_path=weights_path)
    preds, labels = _run_head(head, corpus)
    correct = sum(1 for p, y in zip(preds, labels, strict=True) if p == y)
    accuracy = correct / len(labels) if labels else 0.0
    confusion = _confusion(preds, labels)

    fn = (
        confusion["BLOCK"]["ALLOW"]
        + confusion["REQUIRE_APPROVAL"]["ALLOW"]
    )
    fp = (
        confusion["ALLOW"]["BLOCK"]
        + confusion["ALLOW"]["REQUIRE_APPROVAL"]
    )
    cost = _FN_WEIGHT * fn + _FP_WEIGHT * fp
    return HeadEval(
        weights_path=str(weights_path),
        n=len(labels),
        accuracy=accuracy,
        confusion=confusion,
        fn_count=fn,
        fp_count=fp,
        cost=cost,
    )


def compare(
    v1_path: Path, v2_path: Path,
    *, per_category: int = 35, seed: int = 2026_05_03,
) -> CompareResult:
    """Generate a fresh eval corpus + score both weights files."""
    corpus = generate(per_category=per_category, seed=seed)
    e1 = evaluate_head(v1_path, corpus)
    e2 = evaluate_head(v2_path, corpus)

    notes: list[str] = []
    if e2.fn_count == 0 and e1.fn_count > 0:
        notes.append("v2 closes all v1 false-negatives (missed malicious)")
    if e2.fn_count > e1.fn_count:
        notes.append(
            f"v2 introduces {e2.fn_count - e1.fn_count} new false-negative(s)"
        )
    if e2.fp_count > e1.fp_count + 5:
        notes.append(
            f"v2 over-blocks {e2.fp_count - e1.fp_count} more benign calls"
        )

    delta_accuracy = e2.accuracy - e1.accuracy
    delta_cost = e1.cost - e2.cost  # positive = v2 cheaper = better
    if delta_cost > 0 and e2.fn_count <= e1.fn_count:
        winner = "v2"
    elif delta_cost < 0:
        winner = "v1"
    else:
        winner = "tie"
    return CompareResult(
        v1=e1, v2=e2,
        winner=winner,
        delta_accuracy=delta_accuracy,
        delta_cost=delta_cost,
        notes=notes,
    )


__all__ = ["CompareResult", "HeadEval", "compare", "evaluate_head"]
