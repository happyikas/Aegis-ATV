"""Task intent classifier (PR-η, Phase B Tier 2).

Classifies a TemporalContext into one of 8 high-level task intents
(debug / explore / edit / test / refactor / review / create /
general). The output goes into the TEMPORAL TRAJECTORY narrative
so the sLLM gets a categorical tag along with deviation (PR-ε)
and identity (PR-ι) signals.

This is pure-numpy logistic regression on the same 32-D trajectory
embedding PR-ι defines:

    p(intent | x) = softmax(W @ x + b)

Why a classifier (not just rules)
---------------------------------

* The same shape (a learned linear head) lets us replace the
  hand-tuned default weights with personalised weights extracted
  from a user's own labeled history (`train_from_labeled`).
* Audit can pin classifier outputs to a specific weight revision
  via :data:`CLASSIFIER_HASH`.
* Multiple advisors (heuristic, sLLM-prompted, learned) can all
  speak the same :class:`IntentPrediction` shape.

Honest framing
--------------

The shipped default weights are **hand-tuned**, not data-derived.
They reflect intuitive associations like "high Bash + errors →
debug", "high Grep/Glob + low Edit → explore". An operator with
substantial labeled history runs ``train_from_labeled`` to get a
personalised classifier. Until then, the default gives plausible
soft-labels for narrative use.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from aegis.burnin.trajectory_catalog import EMBEDDING_DIM, embed_trajectory

if TYPE_CHECKING:
    from aegis.atv.temporal import TemporalContext


# Intent vocabulary. Adding new intents requires bumping
# CLASSIFIER_HASH and re-tuning weights.
INTENTS: tuple[str, ...] = (
    "debug",
    "explore",
    "edit",
    "test",
    "refactor",
    "review",
    "create",
    "general",
)
N_INTENTS: int = len(INTENTS)

# Default version pin. Bump on weight retune so audits can pin
# advices to a specific revision.
_CLASSIFIER_VERSION = "intent_classifier_v1"
CLASSIFIER_HASH: str = hashlib.sha3_256(
    _CLASSIFIER_VERSION.encode()
).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IntentPrediction:
    """One classification result.

    Attributes
    ----------
    primary:
        Top-probability intent name.
    primary_confidence:
        Softmax probability for primary, in [0, 1].
    secondary:
        Second-highest intent name (or ``None`` if the head
        produced only one intent).
    secondary_confidence:
        Probability of the secondary.
    full:
        Full probability dict {intent → prob}, summing to ~1.0.
    classifier_kind:
        ``"hand-tuned"`` for the default; ``"trained-lr"`` for
        weights produced by :func:`train_from_labeled`; future
        kinds may follow.
    classifier_hash:
        SHA3-256 of the implementation version. Pin for forensics.
    """

    primary: str
    primary_confidence: float
    secondary: str | None
    secondary_confidence: float
    full: dict[str, float] = field(default_factory=dict)
    classifier_kind: str = "hand-tuned"
    classifier_hash: str = CLASSIFIER_HASH


@dataclass(frozen=True)
class IntentClassifier:
    """Linear classifier: W @ x + b → softmax probabilities."""

    weights: tuple[tuple[float, ...], ...]    # shape (N_INTENTS, EMBEDDING_DIM)
    bias: tuple[float, ...]                   # shape (N_INTENTS,)
    intents: tuple[str, ...] = INTENTS
    embedding_dim: int = EMBEDDING_DIM
    classifier_kind: str = "hand-tuned"
    classifier_hash: str = CLASSIFIER_HASH
    notes: str = ""

    def weight_array(self) -> np.ndarray:
        return np.asarray(self.weights, dtype=np.float32)

    def bias_array(self) -> np.ndarray:
        return np.asarray(self.bias, dtype=np.float32)

    def predict(self, embedding: np.ndarray) -> IntentPrediction:
        """Forward + softmax. Returns IntentPrediction with full
        probability map and primary/secondary picks."""
        if embedding.shape != (self.embedding_dim,):
            raise ValueError(
                f"embedding shape mismatch: expected "
                f"({self.embedding_dim},), got {embedding.shape}"
            )
        w = self.weight_array()
        b = self.bias_array()
        logits = w @ embedding + b
        # Numerically-stable softmax
        logits -= float(logits.max())
        exp = np.exp(logits)
        probs = exp / float(exp.sum())

        order = np.argsort(probs)[::-1]
        primary_idx = int(order[0])
        secondary_idx = int(order[1]) if len(order) >= 2 else None

        full: dict[str, float] = {
            self.intents[i]: float(probs[i])
            for i in range(len(self.intents))
        }

        return IntentPrediction(
            primary=self.intents[primary_idx],
            primary_confidence=float(probs[primary_idx]),
            secondary=(
                self.intents[secondary_idx]
                if secondary_idx is not None else None
            ),
            secondary_confidence=(
                float(probs[secondary_idx])
                if secondary_idx is not None else 0.0
            ),
            full=full,
            classifier_kind=self.classifier_kind,
            classifier_hash=self.classifier_hash,
        )

    def is_usable(self) -> bool:
        """Sanity gate — refuses classifiers whose dimensions don't
        line up with the current embedding scheme."""
        return (
            self.embedding_dim == EMBEDDING_DIM
            and len(self.intents) == N_INTENTS
            and len(self.weights) == N_INTENTS
            and len(self.bias) == N_INTENTS
        )


# ──────────────────────────────────────────────────────────────────────
# Default hand-tuned classifier
# ──────────────────────────────────────────────────────────────────────


def _zero_weights() -> np.ndarray:
    return np.zeros((N_INTENTS, EMBEDDING_DIM), dtype=np.float32)


# Slot indices in the trajectory embedding (mirrors
# trajectory_catalog._BOW_TOOLS layout).
_SLOT = {
    "tokens_delta": 0,
    "tokens_cumulative": 1,
    "cache_hit_rate_mean": 2,
    "cache_hit_rate_drop": 3,
    "n_backtracks": 4,
    "n_redundant": 5,
    "n_errors": 6,
    "n_failures": 7,
    "token_velocity": 8,
    "Read": 9,
    "Edit": 10,
    "Write": 11,
    "MultiEdit": 12,
    "Bash": 13,
    "Grep": 14,
    "Glob": 15,
    "WebFetch": 16,
    "TodoWrite": 17,
    "WebSearch": 18,
    "Task": 19,
    "BashOutput": 20,
    "NotebookEdit": 21,
    "ExitPlanMode": 22,
    "Agent": 23,
    "_other_": 24,
    "_oov_count": 25,
    "n_distinct_tools": 26,
    "success_ratio": 27,
    "is_progress_stalled": 28,
    "cache_hit_rate_final": 29,
}


def default_classifier() -> IntentClassifier:
    """Hand-tuned linear classifier — 8 intents × 32-D embedding.

    Per-intent rationale (positive weights = "this slot pushes
    toward this intent"):

    * **debug**:    high errors / backtracks / Bash + Read
    * **explore**:  high Read / Grep / Glob, mid cache, no errors
    * **edit**:     high Edit / MultiEdit / Read
    * **test**:     high Bash, mid cache, low errors
    * **refactor**: high Edit + Read + low errors
    * **review**:   high Read / Grep, very low Edit
    * **create**:   high Write + Read
    * **general**:  weak prior, picks up the residue
    """
    w = _zero_weights()
    b = np.zeros(N_INTENTS, dtype=np.float32)

    def _set(intent: str, slot: str, value: float) -> None:
        i_idx = INTENTS.index(intent)
        s_idx = _SLOT[slot]
        w[i_idx, s_idx] = value

    # debug
    _set("debug", "n_errors", 4.0)
    _set("debug", "n_backtracks", 3.0)
    _set("debug", "n_failures", 3.0)
    _set("debug", "Bash", 2.0)
    _set("debug", "Read", 1.0)
    _set("debug", "cache_hit_rate_drop", 1.5)
    b[INTENTS.index("debug")] = -0.5

    # explore
    _set("explore", "Grep", 3.0)
    _set("explore", "Glob", 3.0)
    _set("explore", "Read", 2.0)
    _set("explore", "n_distinct_tools", 2.0)
    _set("explore", "Edit", -2.0)
    _set("explore", "n_errors", -1.0)
    b[INTENTS.index("explore")] = -0.3

    # edit
    _set("edit", "Edit", 4.0)
    _set("edit", "MultiEdit", 3.0)
    _set("edit", "Read", 1.5)
    _set("edit", "Bash", -0.5)
    _set("edit", "n_errors", -1.0)
    b[INTENTS.index("edit")] = -0.4

    # test
    _set("test", "Bash", 3.0)
    _set("test", "Read", 1.0)
    _set("test", "BashOutput", 2.0)
    _set("test", "Edit", -0.5)
    _set("test", "n_errors", 0.5)            # tests can fail; weak signal
    b[INTENTS.index("test")] = -0.5

    # refactor
    _set("refactor", "Edit", 3.0)
    _set("refactor", "MultiEdit", 3.5)
    _set("refactor", "Read", 2.0)
    _set("refactor", "Grep", 1.5)
    _set("refactor", "n_errors", -1.5)        # refactor with no behavior
    b[INTENTS.index("refactor")] = -0.7

    # review
    _set("review", "Read", 4.0)
    _set("review", "Grep", 2.5)
    _set("review", "Glob", 1.5)
    _set("review", "Edit", -3.0)
    _set("review", "Write", -2.0)
    b[INTENTS.index("review")] = -0.4

    # create
    _set("create", "Write", 4.0)
    _set("create", "Read", 1.0)
    _set("create", "Edit", 0.5)
    _set("create", "n_errors", -0.5)
    b[INTENTS.index("create")] = -0.6

    # general — weak prior, no strong slot signal
    b[INTENTS.index("general")] = 0.5

    return IntentClassifier(
        weights=tuple(tuple(float(v) for v in row) for row in w),
        bias=tuple(float(v) for v in b),
        intents=INTENTS,
        embedding_dim=EMBEDDING_DIM,
        classifier_kind="hand-tuned",
        classifier_hash=CLASSIFIER_HASH,
        notes=(
            "Hand-tuned weights — replace with train_from_labeled() "
            "output once you have ≥ 50 labeled trajectories."
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Training (pure-numpy logistic regression)
# ──────────────────────────────────────────────────────────────────────


def train_from_labeled(
    embeddings: np.ndarray,
    labels: list[str],
    *,
    n_epochs: int = 200,
    learning_rate: float = 0.05,
    weight_decay: float = 1e-3,
    seed: int = 1337,
) -> IntentClassifier:
    """Train a logistic regression head from labeled trajectory
    embeddings. Pure numpy, deterministic given seed.

    Parameters
    ----------
    embeddings:
        ``(N, EMBEDDING_DIM)`` matrix.
    labels:
        List of length ``N``; each must be one of :data:`INTENTS`.
    n_epochs / learning_rate / weight_decay:
        Standard hyperparameters. Defaults work for ~50–500 sample
        regimes.
    seed:
        RNG for weight init.

    Returns
    -------
    IntentClassifier with ``classifier_kind="trained-lr"``.
    """
    if embeddings.ndim != 2 or embeddings.shape[1] != EMBEDDING_DIM:
        raise ValueError(
            f"embeddings shape must be (N, {EMBEDDING_DIM}), "
            f"got {embeddings.shape}"
        )
    n = embeddings.shape[0]
    if n != len(labels):
        raise ValueError(
            f"embeddings and labels disagree ({n} vs {len(labels)})"
        )
    intent_to_idx = {name: i for i, name in enumerate(INTENTS)}
    for lab in labels:
        if lab not in intent_to_idx:
            raise ValueError(
                f"label {lab!r} not in INTENTS {INTENTS}"
            )

    rng = np.random.default_rng(seed)
    w = rng.normal(0.0, 0.05, size=(N_INTENTS, EMBEDDING_DIM)).astype(np.float32)
    b = np.zeros(N_INTENTS, dtype=np.float32)

    # One-hot Y
    y = np.zeros((n, N_INTENTS), dtype=np.float32)
    for i, lab in enumerate(labels):
        y[i, intent_to_idx[lab]] = 1.0

    x = embeddings.astype(np.float32)

    for _ in range(n_epochs):
        # Forward
        logits = x @ w.T + b      # (N, K)
        logits -= logits.max(axis=1, keepdims=True)
        exp = np.exp(logits)
        probs = exp / exp.sum(axis=1, keepdims=True)

        # Cross-entropy gradient
        grad_logits = (probs - y) / n
        grad_w = grad_logits.T @ x + weight_decay * w
        grad_b = grad_logits.sum(axis=0)

        w -= learning_rate * grad_w
        b -= learning_rate * grad_b

    classifier_hash = hashlib.sha3_256(
        ("trained-lr/" + _CLASSIFIER_VERSION
         + f"/n={n}/seed={seed}").encode()
    ).hexdigest()

    return IntentClassifier(
        weights=tuple(tuple(float(v) for v in row) for row in w),
        bias=tuple(float(v) for v in b),
        intents=INTENTS,
        embedding_dim=EMBEDDING_DIM,
        classifier_kind="trained-lr",
        classifier_hash=classifier_hash,
        notes=f"Trained LR — n={n}, epochs={n_epochs}, lr={learning_rate}",
    )


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


def predict_intent(
    ctx: TemporalContext,
    classifier: IntentClassifier | None = None,
) -> IntentPrediction:
    """Convenience: embed the trajectory and run the classifier.

    Uses :func:`default_classifier` when ``classifier=None``.
    """
    cls = classifier or default_classifier()
    if not cls.is_usable():
        # Degenerate fallback — uniform distribution.
        uniform = 1.0 / N_INTENTS
        return IntentPrediction(
            primary="general",
            primary_confidence=uniform,
            secondary=None,
            secondary_confidence=0.0,
            full={name: uniform for name in INTENTS},
            classifier_kind="degenerate",
            classifier_hash=CLASSIFIER_HASH,
        )
    emb = embed_trajectory(ctx)
    return cls.predict(emb)


def render_intent(prediction: IntentPrediction) -> str:
    """One-block narrative section."""
    lines = [
        "TASK INTENT",
        f"  primary:   {prediction.primary} "
        f"({prediction.primary_confidence * 100:.0f}% confidence)",
    ]
    if prediction.secondary and prediction.secondary_confidence > 0.05:
        lines.append(
            f"  secondary: {prediction.secondary} "
            f"({prediction.secondary_confidence * 100:.0f}%)"
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


def classifier_to_dict(c: IntentClassifier) -> dict[str, Any]:
    return {
        "weights": [list(row) for row in c.weights],
        "bias": list(c.bias),
        "intents": list(c.intents),
        "embedding_dim": c.embedding_dim,
        "classifier_kind": c.classifier_kind,
        "classifier_hash": c.classifier_hash,
        "notes": c.notes,
    }


def classifier_from_dict(d: dict[str, Any]) -> IntentClassifier:
    weights_raw = d.get("weights") or []
    weights = tuple(
        tuple(float(v) for v in row)
        for row in weights_raw
    )
    bias_raw = d.get("bias") or []
    return IntentClassifier(
        weights=weights,
        bias=tuple(float(v) for v in bias_raw),
        intents=tuple(d.get("intents") or INTENTS),
        embedding_dim=int(d.get("embedding_dim", EMBEDDING_DIM)),
        classifier_kind=str(d.get("classifier_kind", "hand-tuned")),
        classifier_hash=str(d.get("classifier_hash", "")),
        notes=str(d.get("notes", "")),
    )


def save_classifier(classifier: IntentClassifier, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(classifier_to_dict(classifier), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_classifier(path: Path) -> IntentClassifier:
    if not path.is_file():
        raise FileNotFoundError(
            f"intent classifier not at {path}"
        )
    return classifier_from_dict(
        json.loads(path.read_text(encoding="utf-8"))
    )


def default_classifier_path() -> Path:
    """Honours ``AEGIS_INTENT_CLASSIFIER_PATH`` env override; falls
    back to ``~/.aegis/intent_classifier.json``; final fallback to
    the shipped ``models/intent_classifier_v1.json``."""
    import os

    override = os.environ.get(
        "AEGIS_INTENT_CLASSIFIER_PATH", "",
    ).strip()
    if override:
        return Path(override).expanduser()
    home_path = Path.home() / ".aegis" / "intent_classifier.json"
    if home_path.is_file():
        return home_path
    return (
        Path(__file__).resolve().parents[2].parents[0]
        / "models" / "intent_classifier_v1.json"
    )


def load_classifier_or_default(
    path: Path | None = None,
) -> IntentClassifier:
    """Best-effort load → falls back to :func:`default_classifier`.
    Never raises; produces something usable."""
    p = path or default_classifier_path()
    try:
        cls = load_classifier(p)
        if cls.is_usable():
            return cls
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        pass
    return default_classifier()


# Stable timestamp helper for any caller that wants to record when
# a classifier was trained / loaded.
def now_ns() -> int:
    return time.time_ns()


__all__ = [
    "CLASSIFIER_HASH",
    "INTENTS",
    "IntentClassifier",
    "IntentPrediction",
    "N_INTENTS",
    "classifier_from_dict",
    "classifier_to_dict",
    "default_classifier",
    "default_classifier_path",
    "load_classifier",
    "load_classifier_or_default",
    "now_ns",
    "predict_intent",
    "render_intent",
    "save_classifier",
    "train_from_labeled",
]
