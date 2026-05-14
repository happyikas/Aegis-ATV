"""Human adjudication labels — ground-truth overlay for the shadow corpus.

The patent (¶[0083]) names four sources of labels for the sLLM
training corpus:

    1. Human analyst adjudication        ← THIS MODULE
    2. Post-hoc incident labeling
    3. Red-team simulation
    4. Counterfactual synthesis

Until this module landed, ``shadow.jsonl`` held only the firewall's
*own* verdicts — useful for distillation but indistinguishable from
self-training. ``labels.jsonl`` is a separate file where humans
adjudicate specific records, joined back to the shadow corpus by
``trace_id`` / ``invocation_id`` during ``aegis burnin train-m13``.

Wire format
-----------
One JSON object per line at ``$AEGIS_LABELS_PATH`` (env override) or
``~/.aegis/labels.jsonl``::

    {
      "ts_ns":          1714737610123456789,
      "trace_id":       "<trace_id from audit/shadow record>",
      "invocation_id":  "<optional, when trace_id is unknown>",
      "label":          "benign" | "suspicious" | "malicious",
      "verdict":        "ALLOW" | "REQUIRE_APPROVAL" | "BLOCK",
      "reason":         "<freeform analyst note, max 500 chars>",
      "analyst":        "<handle / email / 'cli'>",
      "confidence":     0.92,
      "schema_version": 1
    }

Re-labeling: appending a new record with the same ``trace_id`` is
explicit "supersede" — readers should treat the latest ``ts_ns`` as
authoritative. We never rewrite history.

Patent label class ↔ firewall verdict class
-------------------------------------------
The patent uses ``normal / suspicious / malicious``; the firewall
verdict class is ``ALLOW / REQUIRE_APPROVAL / BLOCK``. These map 1:1:

    benign     ↔ ALLOW
    suspicious ↔ REQUIRE_APPROVAL
    malicious  ↔ BLOCK

The CLI accepts both vocabularies and canonicalises both into the
record so downstream consumers can read whichever they prefer
without parsing fallbacks.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── canonical class mapping ────────────────────────────────────────

LABEL_TO_VERDICT: dict[str, str] = {
    "benign": "ALLOW",
    "suspicious": "REQUIRE_APPROVAL",
    "malicious": "BLOCK",
}

VERDICT_TO_LABEL: dict[str, str] = {v: k for k, v in LABEL_TO_VERDICT.items()}

VALID_LABELS: frozenset[str] = frozenset(LABEL_TO_VERDICT.keys())
VALID_VERDICTS: frozenset[str] = frozenset(LABEL_TO_VERDICT.values())

SCHEMA_VERSION = 1
MAX_REASON_LEN = 500


# ── persistence path ──────────────────────────────────────────────


def labels_path() -> Path:
    """Returns the labels.jsonl path.

    Override via ``AEGIS_LABELS_PATH``; default
    ``~/.aegis/labels.jsonl``. Mirrors ``shadow._shadow_log_path``
    so the two files sit beside each other for trivial joining.
    """
    raw = os.environ.get("AEGIS_LABELS_PATH", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".aegis" / "labels.jsonl"


# ── record shape ──────────────────────────────────────────────────


@dataclass(frozen=True)
class LabelRecord:
    """One human adjudication. Immutable; supersede by appending a
    new record with the same ``trace_id`` and a later ``ts_ns``."""

    ts_ns: int
    trace_id: str
    invocation_id: str
    label: str
    verdict: str
    reason: str
    analyst: str
    confidence: float
    schema_version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_ns": self.ts_ns,
            "trace_id": self.trace_id,
            "invocation_id": self.invocation_id,
            "label": self.label,
            "verdict": self.verdict,
            "reason": self.reason,
            "analyst": self.analyst,
            "confidence": self.confidence,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LabelRecord:
        return cls(
            ts_ns=int(d["ts_ns"]),
            trace_id=str(d.get("trace_id", "")),
            invocation_id=str(d.get("invocation_id", "")),
            label=str(d["label"]),
            verdict=str(d["verdict"]),
            reason=str(d.get("reason", "")),
            analyst=str(d.get("analyst", "cli")),
            confidence=float(d.get("confidence", 1.0)),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


# ── normalisation ─────────────────────────────────────────────────


class LabelError(ValueError):
    """Raised for unrecognised label / verdict input."""


def canonicalise(
    *, label: str | None = None, verdict: str | None = None,
) -> tuple[str, str]:
    """Return ``(label, verdict)`` from either input, validating both.

    Exactly one of ``label`` / ``verdict`` must be non-None. Accepts
    case-insensitive input. Raises :class:`LabelError` on invalid
    values or ambiguous input.
    """
    if (label is None) == (verdict is None):
        raise LabelError(
            "exactly one of label / verdict must be provided"
        )
    if label is not None:
        lab = label.strip().lower()
        if lab not in VALID_LABELS:
            raise LabelError(
                f"unknown label {label!r}; "
                f"expected one of {sorted(VALID_LABELS)}"
            )
        return lab, LABEL_TO_VERDICT[lab]
    assert verdict is not None
    ver = verdict.strip().upper()
    if ver not in VALID_VERDICTS:
        raise LabelError(
            f"unknown verdict {verdict!r}; "
            f"expected one of {sorted(VALID_VERDICTS)}"
        )
    return VERDICT_TO_LABEL[ver], ver


# ── write ─────────────────────────────────────────────────────────


def append_label(
    *,
    trace_id: str,
    label: str | None = None,
    verdict: str | None = None,
    invocation_id: str = "",
    reason: str = "",
    analyst: str = "cli",
    confidence: float = 1.0,
    ts_ns: int | None = None,
    path: Path | None = None,
) -> LabelRecord:
    """Append one adjudication. Returns the persisted record.

    ``trace_id`` is required (the join key to the shadow corpus and
    audit chain). Empty strings are rejected — use ``--last`` in the
    CLI to look up the trace_id from the audit log if needed.

    Errors during write are NOT swallowed (unlike shadow recording)
    — a human adjudication that silently failed would be worse than
    one that loudly errored.
    """
    if not trace_id.strip():
        raise LabelError("trace_id is required (cannot be empty)")
    lab, ver = canonicalise(label=label, verdict=verdict)
    if not 0.0 <= confidence <= 1.0:
        raise LabelError(
            f"confidence must be in [0, 1]; got {confidence!r}"
        )
    rec = LabelRecord(
        ts_ns=ts_ns if ts_ns is not None else time.time_ns(),
        trace_id=trace_id.strip(),
        invocation_id=invocation_id.strip(),
        label=lab,
        verdict=ver,
        reason=(reason or "")[:MAX_REASON_LEN],
        analyst=analyst.strip() or "cli",
        confidence=float(confidence),
        schema_version=SCHEMA_VERSION,
    )
    p = path if path is not None else labels_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec.to_dict(), sort_keys=True) + "\n")
    return rec


# ── read ──────────────────────────────────────────────────────────


def read_labels(path: Path | None = None) -> list[LabelRecord]:
    """Read every label record in order of file appearance.

    Returns ``[]`` if the file doesn't exist. Malformed lines are
    skipped (logged to stderr in CLI usage; silent here to match
    ``shadow.read_corpus`` semantics).
    """
    p = path if path is not None else labels_path()
    if not p.exists():
        return []
    out: list[LabelRecord] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(LabelRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return out


def iter_labels(path: Path | None = None) -> Iterator[LabelRecord]:
    """Yield label records one at a time. Memory-friendly for large files."""
    p = path if path is not None else labels_path()
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield LabelRecord.from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue


def latest_label_for(trace_id: str, path: Path | None = None) -> LabelRecord | None:
    """Return the most-recent (by ``ts_ns``) label for ``trace_id``,
    or ``None`` if no record matches.

    Used by the trainer to resolve "what's the human-adjudicated
    label for this shadow row" — the freshest label wins, supporting
    re-labeling without rewriting history.
    """
    best: LabelRecord | None = None
    for rec in iter_labels(path):
        if rec.trace_id != trace_id:
            continue
        if best is None or rec.ts_ns > best.ts_ns:
            best = rec
    return best


def labels_by_trace(path: Path | None = None) -> dict[str, LabelRecord]:
    """Return a {trace_id: latest_label} dict. Convenience wrapper
    for joining against the shadow corpus in bulk."""
    out: dict[str, LabelRecord] = {}
    for rec in iter_labels(path):
        existing = out.get(rec.trace_id)
        if existing is None or rec.ts_ns > existing.ts_ns:
            out[rec.trace_id] = rec
    return out
