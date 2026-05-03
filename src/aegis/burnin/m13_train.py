"""M13 attribution-head v2 weight trainer (Burn-in Shadow / synthetic).

Replaces the hand-tuned v1 weights with weights learned from a labelled
corpus. Preserves v1's "frozen linear classifier" architecture (Claim
8) — the only thing that changes is the 30 floats in
``subfield_weights`` and the two thresholds.

Pipeline
--------
1. **Feature extraction.** For each :class:`LabeledExample`, run
   ``build_atv(inp)`` to produce the 2080-D vector, then compute the
   30-D ``base`` vector exactly the way :class:`AttributionHead` does
   at inference (max-aggregator combined with the v1 named-slot
   weights). This is the input to the linear model — a 30-vector
   that's already normalised to [0, 1] per slot.

2. **Label encoding.** Map the three classes to scalar targets aligned
   with v1's threshold geometry::

       ALLOW             → 0.20  (well below approval threshold 0.55)
       REQUIRE_APPROVAL  → 0.62  (between approval and block)
       BLOCK             → 0.85  (above block threshold 0.70)

   This frames the task as a **regression** problem so the same code
   path scores all three classes; the weights describe a single
   linear function over the 30 features and the head's existing
   threshold-comparison code separates the ranges at inference.

3. **Class-balanced non-negative least squares.** Weight each example
   by ``1 / class_count`` so 105 BLOCKs don't dominate 35 ALLOWs.
   Solve the resulting weighted-NNLS via projected gradient descent
   — a simple loop, no scipy dependency.

4. **Threshold calibration.** Once weights are fit, scan a small grid
   of (approval, block) thresholds and pick the pair maximising
   held-out 3-class accuracy (subject to ``approval < block``).

5. **Output.** Write a v2 JSON file in the same schema as
   ``models/m13_attribution_head_v1.json``. Recomputing the SHA3 over
   the new file gives the v2 ``model_hash``.

Determinism: numpy's ``np.linalg.lstsq`` and our projected-gradient
loop are deterministic; the synthetic corpus is seeded; same input →
same v2 weights bit-for-bit.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from aegis.atv.builder import build_atv
from aegis.burnin.m13_data import LabeledExample
from aegis.judge.attribution_head import (
    DEFAULT_WEIGHTS_PATH,
    _aggregate_subfield,
    _load_weights,
    _named_slot_score,
)
from aegis.schema import ALL_SUBFIELDS

# Regression targets (see module docstring). These bracket v1's 0.55 /
# 0.70 thresholds so a perfectly-fit model puts each class on the
# right side of the threshold the head already checks.
_LABEL_TARGETS = {
    "ALLOW":            0.20,
    "REQUIRE_APPROVAL": 0.62,
    "BLOCK":            0.85,
}


# ─────────────────────────────────────────────────────────────────────
# Feature extraction
# ─────────────────────────────────────────────────────────────────────


def extract_features(example: LabeledExample) -> np.ndarray:
    """Compute the 30-D ``base`` vector that v1's head sums over.

    Mirrors :meth:`AttributionHead.evaluate_full` exactly — same
    aggregator, same named-slot fallback, same v1 slot weights — so
    the trained weights are interchangeable with v1's weights at
    inference. Only the per-subfield scalar weights themselves change.
    """
    weights = _load_weights(str(DEFAULT_WEIGHTS_PATH))
    atv = build_atv(example.inp)
    feats = np.zeros(len(ALL_SUBFIELDS), dtype=np.float32)
    for i, (name, slc) in enumerate(ALL_SUBFIELDS):
        sf_arr = atv[slc]
        base = _aggregate_subfield(sf_arr)
        if name in weights.named_slot_weights:
            base = max(
                base,
                _named_slot_score(sf_arr, weights.named_slot_weights[name]),
            )
        feats[i] = float(base)
    return feats


def build_design_matrix(
    corpus: list[LabeledExample],
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Stack features + targets across the corpus.

    Returns ``(X, y, subfield_names)``. ``X`` is ``(N, 30)``, ``y`` is
    ``(N,)`` (regression target per example), ``subfield_names`` is the
    ordered list of subfield names matching the columns of ``X``.
    """
    X = np.stack([extract_features(ex) for ex in corpus]).astype(np.float64)
    y = np.array(
        [_LABEL_TARGETS[ex.label] for ex in corpus], dtype=np.float64,
    )
    names = [name for name, _slc in ALL_SUBFIELDS]
    return X, y, names


# ─────────────────────────────────────────────────────────────────────
# Class-balanced non-negative least squares
# ─────────────────────────────────────────────────────────────────────


def _class_weights(labels: list[str]) -> np.ndarray:
    """Inverse-frequency sample weight so 3-class imbalance doesn't
    dominate the loss."""
    n = len(labels)
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return np.array(
        [n / (3.0 * counts[label]) for label in labels],
        dtype=np.float64,
    )


def fit_nnls(
    X: np.ndarray, y: np.ndarray, sample_weight: np.ndarray,
    *, n_iter: int = 20_000, lr: float | None = None, l2: float = 1e-5,
) -> np.ndarray:
    """Weighted non-negative least squares via projected gradient.

    Minimises ``sum_i w_i (X_i · β − y_i)^2 + l2 · ‖β‖²`` subject to
    ``β ≥ 0``. The L2 term is tiny (1e-5) — just enough to keep the
    Hessian well-conditioned without over-shrinking signal-bearing
    weights like ``tool_arg_inspection``. The projection step
    ``β := max(β, 0)`` enforces non-negativity.

    Learning-rate auto-tuning: when ``lr=None`` (default) we set
    ``lr = 1 / (2 · λ_max(X^T diag(w) X) + 2 · l2)`` so the gradient
    step is contractive in the largest eigen-direction. Empirically
    this converges in ~20k iters on 30-feature problems; manual
    tuning is rarely needed.

    Why projected gradient (not the closed-form normal equations +
    clamp): the projection makes the unconstrained solution sub-
    optimal when correlated features negatively interact —
    projected GD finds the true constrained minimum, while a post-
    hoc clamp would over-zero correlated features.
    """
    rng = np.random.default_rng(seed=2026_05_03)
    n_features = X.shape[1]
    beta = rng.uniform(0.0, 0.05, size=n_features)
    sw = sample_weight[:, None]   # (N, 1) → broadcast to (N, F)
    XtWX = X.T @ (X * sw)         # (F, F) Gram-with-weights
    XtWy = X.T @ (y * sample_weight)
    if lr is None:
        # Spectral-radius-bounded step: 1/(2 L) where L = λ_max(2·XtWX) + 2·l2.
        eigmax = float(np.linalg.eigvalsh(XtWX)[-1])
        L = 2.0 * eigmax + 2.0 * l2
        lr = 1.0 / max(L, 1e-9)
    for _ in range(n_iter):
        grad = 2.0 * (XtWX @ beta - XtWy) + 2.0 * l2 * beta
        beta = beta - lr * grad
        beta = np.clip(beta, 0.0, None)
    return beta


# ─────────────────────────────────────────────────────────────────────
# Threshold calibration
# ─────────────────────────────────────────────────────────────────────


def _decide(score: float, t_appr: float, t_block: float) -> str:
    if score >= t_block:
        return "BLOCK"
    if score >= t_appr:
        return "REQUIRE_APPROVAL"
    return "ALLOW"


def calibrate_thresholds(
    X: np.ndarray, y_labels: list[str], beta: np.ndarray,
) -> tuple[float, float]:
    """Grid-search ``(t_approval, t_block)`` maximising 3-class accuracy.

    Search range covers 0.30..0.90 in 0.025 increments — enough
    granularity for the regression-target geometry described in the
    module docstring without exploding the loop count.
    """
    scores = X @ beta
    grid = np.arange(0.30, 0.90 + 1e-9, 0.025)
    best = (0.0, 0.55, 0.70)  # (acc, t_approval, t_block)
    for t_appr in grid:
        for t_block in grid:
            if t_block <= t_appr:
                continue
            preds = [_decide(float(s), float(t_appr), float(t_block)) for s in scores]
            correct = sum(1 for p, y in zip(preds, y_labels, strict=True) if p == y)
            acc = correct / len(y_labels)
            if acc > best[0]:
                best = (acc, float(t_appr), float(t_block))
    return best[1], best[2]


# ─────────────────────────────────────────────────────────────────────
# Train + persist
# ─────────────────────────────────────────────────────────────────────


@dataclass
class TrainResult:
    """Returned by :func:`train_v2` — weights + thresholds + report."""

    subfield_weights: dict[str, float]
    threshold_block: float
    threshold_approval: float
    n_samples: int
    n_train: int
    n_test: int
    train_accuracy: float
    test_accuracy: float
    confusion_train: dict[str, dict[str, int]] = field(default_factory=dict)
    confusion_test: dict[str, dict[str, int]] = field(default_factory=dict)


def _confusion_matrix(
    preds: list[str], labels: list[str],
) -> dict[str, dict[str, int]]:
    classes = ("ALLOW", "REQUIRE_APPROVAL", "BLOCK")
    out: dict[str, dict[str, int]] = {c: dict.fromkeys(classes, 0) for c in classes}
    for p, y in zip(preds, labels, strict=True):
        out[y][p] += 1
    return out


def _accuracy(
    X: np.ndarray, beta: np.ndarray, y_labels: list[str],
    t_appr: float, t_block: float,
) -> tuple[float, list[str]]:
    scores = X @ beta
    preds = [_decide(float(s), t_appr, t_block) for s in scores]
    correct = sum(1 for p, y in zip(preds, y_labels, strict=True) if p == y)
    return correct / len(y_labels), preds


def train_v2(
    corpus: list[LabeledExample], *, test_fraction: float = 0.2,
) -> TrainResult:
    """Train M13 v2 on ``corpus``. Returns weights + held-out accuracy.

    The corpus is split deterministically: every 5th example
    (``test_fraction=0.2``) goes to the held-out test set, the rest to
    training. This avoids fitting on data we'll evaluate on — the
    test_accuracy is what we report as v2's improvement over v1.
    """
    if not corpus:
        raise ValueError("corpus is empty")
    if not 0 < test_fraction < 1:
        raise ValueError("test_fraction must be in (0, 1)")

    # Deterministic split — corpus is already shuffled once at generation.
    test_stride = int(round(1.0 / test_fraction))
    train: list[LabeledExample] = []
    test: list[LabeledExample] = []
    for i, ex in enumerate(corpus):
        (test if i % test_stride == 0 else train).append(ex)

    X_tr, y_tr, names = build_design_matrix(train)
    X_te, _y_te, _ = build_design_matrix(test)
    y_tr_labels = [ex.label for ex in train]
    y_te_labels = [ex.label for ex in test]

    sw = _class_weights(y_tr_labels)
    beta = fit_nnls(X_tr, y_tr, sw)
    t_appr, t_block = calibrate_thresholds(X_tr, y_tr_labels, beta)

    train_acc, tr_preds = _accuracy(X_tr, beta, y_tr_labels, t_appr, t_block)
    test_acc, te_preds = _accuracy(X_te, beta, y_te_labels, t_appr, t_block)

    return TrainResult(
        subfield_weights={names[i]: float(beta[i]) for i in range(len(names))},
        threshold_block=t_block,
        threshold_approval=t_appr,
        n_samples=len(corpus),
        n_train=len(train),
        n_test=len(test),
        train_accuracy=train_acc,
        test_accuracy=test_acc,
        confusion_train=_confusion_matrix(tr_preds, y_tr_labels),
        confusion_test=_confusion_matrix(te_preds, y_te_labels),
    )


def write_v2_json(
    result: TrainResult,
    out_path: Path,
    *,
    base_v1_path: Path = DEFAULT_WEIGHTS_PATH,
) -> str:
    """Write the trained weights to ``out_path`` in v1's schema.

    Inherits ``named_slot_weights`` and ``feature_aggregator`` from the
    v1 manifest verbatim (the trainer doesn't re-learn those — the v1
    sub-feature weights are still the right thing for the encoder
    side). Stamps a fresh ``model_hash_seed`` and a ``_provenance``
    block recording train/test accuracy + sample counts so audit
    consumers can distinguish v1 from v2 at a glance.

    Returns the SHA3-256 of the written file (the v2 ``model_hash``).
    """
    v1_manifest: dict = json.loads(base_v1_path.read_bytes().decode("utf-8"))
    v2_manifest: dict = {
        "_comment": (
            "M13 attribution head v2 — weights learned from labelled "
            "(ATV, verdict) corpus via class-balanced non-negative least "
            "squares (aegis.burnin.m13_train). Same architecture as v1, "
            "same encoder-side named-slot weights, only the 30 "
            "subfield-aggregate weights and the two thresholds are "
            "retrained. Bit-identical inference path through "
            "AttributionHead — drop-in replacement."
        ),
        "version": 2,
        "schema_version": v1_manifest.get("schema_version", "ATV-2080-v1"),
        "feature_aggregator": v1_manifest.get(
            "feature_aggregator", "sum_of_named_slots",
        ),
        "subfield_weights": {
            name: round(w, 6) for name, w in result.subfield_weights.items()
        },
        "named_slot_weights": v1_manifest.get("named_slot_weights", {}),
        "thresholds": {
            "block":             round(result.threshold_block, 4),
            "require_approval":  round(result.threshold_approval, 4),
        },
        "model_hash_seed": (
            f"m13-attribution-head-v2-{time.strftime('%Y-%m-%d')}"
        ),
        "_provenance": {
            "trained_by": "aegis.burnin.m13_train",
            "training_data": (
                f"synthetic corpus (n={result.n_samples}, "
                f"train={result.n_train}, test={result.n_test})"
            ),
            "train_accuracy": round(result.train_accuracy, 4),
            "test_accuracy": round(result.test_accuracy, 4),
            "confusion_test": result.confusion_test,
            "predecessor": v1_manifest.get(
                "model_hash_seed", "m13-attribution-head-v1",
            ),
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(v2_manifest, indent=2, sort_keys=False) + "\n"
    out_path.write_text(raw, encoding="utf-8")
    return hashlib.sha3_256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "TrainResult",
    "build_design_matrix",
    "calibrate_thresholds",
    "extract_features",
    "fit_nnls",
    "train_v2",
    "write_v2_json",
]
