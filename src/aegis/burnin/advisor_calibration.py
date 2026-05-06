"""Advisor-gate calibration — burn-in derived percentile thresholds for
the M13 confidence and session-drift signals (v2.6, PR-ψ-calibration).

Background
----------

The v2.5.1 critical-moment gate (`tools/aegis_local_hook.py:_should_invoke_advisor`)
deliberately omitted two candidate signals from its 5-trigger set:

  * M13 attribution-head confidence — natural distribution centred
    around 0.3-0.5 even on routine ALLOW, so a static "below 0.7"
    threshold fires on every call.
  * Session topic drift — bge-local dependent and skewed; meaningful
    only relative to the agent's own historical baseline.

Both signals are *informative* once you have a baseline distribution
to compare against. This module ships that baseline.

What it does
------------

* :class:`AdvisorCalibration` — frozen dataclass with low-percentile
  thresholds for ``m13_score`` and high-percentile thresholds for
  ``topic_drift``.
* :func:`extract_calibration_from_audit` — walks an ``audit.jsonl``,
  pulls the relevant fields out of each ``explain`` block, and computes
  percentiles via the same algorithm as :mod:`aegis.burnin.anomaly`.
* :func:`default_calibration` — synthesised fallback covering the
  "fresh install, no audit history" case. Conservative thresholds so
  the gate doesn't fire on routine traffic.
* JSON I/O + ``models/advisor_calibration_v1.json`` shipped default.

The hook uses ``load_calibration_or_default`` (matching the pattern of
:func:`aegis.burnin.anomaly.load_baseline_or_default`). When the loaded
calibration's ``n_sessions`` is below ``MIN_SAMPLES_FOR_CALIBRATION``,
:meth:`AdvisorCalibration.is_usable` returns False and the hook leaves
the two signals out of the gate — preserving v2.5.1 behaviour for
under-trained installs.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Below this many calibration samples the percentile estimates are too
# noisy to gate on. Matches MIN_SAMPLES_FOR_BASELINE convention.
MIN_SAMPLES_FOR_CALIBRATION: int = 5

_DEFAULT_CALIBRATION_FILENAME = "advisor_calibration.json"


# ──────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AdvisorCalibration:
    """Per-metric percentile thresholds for the v2.6 advisor gate.

    The gate consults two new signals:

    * ``m13_score < m13_score_p10`` → "M13 confidence is in the bottom
      decile of historical baseline" (model itself is unusually
      uncertain).
    * ``topic_drift > topic_drift_p95`` → "session topic drift is in
      the top 5% of historical baseline" (agent is wandering).

    Lower-percentile fields for M13 and upper-percentile fields for
    drift reflect that the *informative* tail is opposite for each.
    """

    version: int
    n_sessions: int                # total samples that fed the percentiles
    extracted_at_ns: int
    extracted_from: str

    # M13 confidence — informative tail is the LOW end.
    m13_score_n: int
    m13_score_p10: float
    m13_score_p25: float
    m13_score_p50: float

    # Session topic drift — informative tail is the HIGH end.
    topic_drift_n: int
    topic_drift_p50: float
    topic_drift_p75: float
    topic_drift_p90: float
    topic_drift_p95: float

    notes: str = ""

    def is_usable(self) -> bool:
        """A calibration with too few samples in EITHER metric produces
        unreliable thresholds. The gate falls back to v2.5.1 behaviour
        (signals 6 & 7 disabled) when this is False."""
        return (
            self.n_sessions >= MIN_SAMPLES_FOR_CALIBRATION
            and self.m13_score_n >= MIN_SAMPLES_FOR_CALIBRATION
            and self.topic_drift_n >= MIN_SAMPLES_FOR_CALIBRATION
        )

    def stable_hash(self) -> str:
        """SHA3-256 over the percentile vector. Stamped into the gate
        decision when this calibration drives a fire, so audit replay
        can verify the same thresholds were used."""
        payload = json.dumps(
            {
                "version": self.version,
                "m13_score_p10": round(self.m13_score_p10, 6),
                "m13_score_p25": round(self.m13_score_p25, 6),
                "m13_score_p50": round(self.m13_score_p50, 6),
                "topic_drift_p50": round(self.topic_drift_p50, 6),
                "topic_drift_p75": round(self.topic_drift_p75, 6),
                "topic_drift_p90": round(self.topic_drift_p90, 6),
                "topic_drift_p95": round(self.topic_drift_p95, 6),
            },
            sort_keys=True,
        )
        return hashlib.sha3_256(payload.encode()).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# Percentile primitives — same linear-interpolation as anomaly.py
# ──────────────────────────────────────────────────────────────────────


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile. ``p`` in [0, 1]."""
    n = len(values)
    if n == 0:
        return 0.0
    sorted_vals = sorted(values)
    if n == 1:
        return float(sorted_vals[0])
    idx = (n - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = idx - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


# ──────────────────────────────────────────────────────────────────────
# Default — used when no real audit history is available
# ──────────────────────────────────────────────────────────────────────


def default_calibration() -> AdvisorCalibration:
    """Conservative shipped defaults — based on a synthesised burn-in
    of dummy-judge sessions (M13 attribution head naturally outputs
    ~0.3-0.5 confidence on routine ALLOW; session_drift is mostly 0
    until the user starts running BGE-local with real embeddings).

    These are deliberately conservative so the new gate signals don't
    fire on routine traffic out of the box. Users with a populated
    audit history should run the extractor against their own data
    for personalised thresholds.
    """
    return AdvisorCalibration(
        version=1,
        n_sessions=50,
        extracted_at_ns=0,
        extracted_from="synthetic-default",
        m13_score_n=200,
        m13_score_p10=0.15,
        m13_score_p25=0.25,
        m13_score_p50=0.40,
        topic_drift_n=200,
        topic_drift_p50=0.05,
        topic_drift_p75=0.20,
        topic_drift_p90=0.45,
        topic_drift_p95=0.70,
        notes=(
            "Conservative defaults — retrain with "
            "`extract_calibration_from_audit(~/.aegis/audit.jsonl)` "
            "after the agent has accumulated >=20 sessions of audit "
            "history for personalised thresholds."
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Extractor — walk audit.jsonl and compute percentiles
# ──────────────────────────────────────────────────────────────────────


def _stream_jsonl(path: Path) -> Any:
    """Yield each well-formed JSON record. Skip blanks / decode
    errors. Mirrors the never-crash contract used elsewhere."""
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def extract_calibration_from_audit(
    audit_path: Path,
    *,
    extracted_at_ns: int = 0,
    min_samples_per_metric: int = MIN_SAMPLES_FOR_CALIBRATION,
) -> AdvisorCalibration | None:
    """Walk an ``audit.jsonl`` and compute per-metric percentiles.

    Reads two fields from each ``explain`` block:

    * ``m13_score`` — present whenever the M13 attribution head ran.
    * ``session_drift.topic_drift`` — present when bge-local + a
      configured session has accumulated drift history.

    Returns ``None`` if either metric has fewer than
    ``min_samples_per_metric`` observations. The hook's
    :func:`load_calibration_or_default` then falls back to the shipped
    default."""
    m13_scores: list[float] = []
    drift_scores: list[float] = []
    session_ids: set[str] = set()

    for rec in _stream_jsonl(audit_path):
        if not isinstance(rec, dict):
            continue
        explain = rec.get("explain") or {}
        if not isinstance(explain, dict):
            continue

        aid = rec.get("aid")
        if isinstance(aid, str):
            session_ids.add(aid)

        m13 = explain.get("m13_score")
        if isinstance(m13, (int, float)):
            m13_scores.append(float(m13))

        drift = explain.get("session_drift")
        if isinstance(drift, dict):
            t = drift.get("topic_drift")
            if isinstance(t, (int, float)):
                drift_scores.append(float(t))

    if (
        len(m13_scores) < min_samples_per_metric
        or len(drift_scores) < min_samples_per_metric
    ):
        return None

    return AdvisorCalibration(
        version=1,
        n_sessions=len(session_ids),
        extracted_at_ns=extracted_at_ns,
        extracted_from=f"audit:{audit_path}",
        m13_score_n=len(m13_scores),
        m13_score_p10=_percentile(m13_scores, 0.10),
        m13_score_p25=_percentile(m13_scores, 0.25),
        m13_score_p50=_percentile(m13_scores, 0.50),
        topic_drift_n=len(drift_scores),
        topic_drift_p50=_percentile(drift_scores, 0.50),
        topic_drift_p75=_percentile(drift_scores, 0.75),
        topic_drift_p90=_percentile(drift_scores, 0.90),
        topic_drift_p95=_percentile(drift_scores, 0.95),
    )


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


def calibration_to_dict(c: AdvisorCalibration) -> dict[str, Any]:
    return {
        "version": c.version,
        "n_sessions": c.n_sessions,
        "extracted_at_ns": c.extracted_at_ns,
        "extracted_from": c.extracted_from,
        "m13_score_n": c.m13_score_n,
        "m13_score_p10": c.m13_score_p10,
        "m13_score_p25": c.m13_score_p25,
        "m13_score_p50": c.m13_score_p50,
        "topic_drift_n": c.topic_drift_n,
        "topic_drift_p50": c.topic_drift_p50,
        "topic_drift_p75": c.topic_drift_p75,
        "topic_drift_p90": c.topic_drift_p90,
        "topic_drift_p95": c.topic_drift_p95,
        "notes": c.notes,
    }


def calibration_from_dict(d: dict[str, Any]) -> AdvisorCalibration:
    return AdvisorCalibration(
        version=int(d.get("version", 1)),
        n_sessions=int(d.get("n_sessions", 0)),
        extracted_at_ns=int(d.get("extracted_at_ns", 0)),
        extracted_from=str(d.get("extracted_from", "")),
        m13_score_n=int(d.get("m13_score_n", 0)),
        m13_score_p10=float(d.get("m13_score_p10", 0.0)),
        m13_score_p25=float(d.get("m13_score_p25", 0.0)),
        m13_score_p50=float(d.get("m13_score_p50", 0.0)),
        topic_drift_n=int(d.get("topic_drift_n", 0)),
        topic_drift_p50=float(d.get("topic_drift_p50", 0.0)),
        topic_drift_p75=float(d.get("topic_drift_p75", 0.0)),
        topic_drift_p90=float(d.get("topic_drift_p90", 0.0)),
        topic_drift_p95=float(d.get("topic_drift_p95", 0.0)),
        notes=str(d.get("notes", "")),
    )


def save_calibration(c: AdvisorCalibration, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(calibration_to_dict(c), indent=2) + "\n",
        encoding="utf-8",
    )


def load_calibration(path: Path) -> AdvisorCalibration:
    return calibration_from_dict(
        json.loads(path.read_text(encoding="utf-8"))
    )


def default_calibration_path() -> Path:
    """Resolve the shipped default ``models/advisor_calibration_v1.json``
    location. Kept consistent with :func:`aegis.burnin.anomaly.default_baseline_path`."""
    here = Path(__file__).resolve()
    repo_root = here.parents[3]  # src/aegis/burnin → repo
    return repo_root / "models" / "advisor_calibration_v1.json"


def load_calibration_or_default(
    path: Path | None = None,
) -> AdvisorCalibration:
    """Best-effort load. Falls back to :func:`default_calibration` when
    the path is missing or malformed — never raises so the hook can
    consult the result unconditionally."""
    p = path or default_calibration_path()
    try:
        return load_calibration(p)
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return default_calibration()


# ──────────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────────


def render_calibration(c: AdvisorCalibration) -> str:
    """Operator-readable summary."""
    usable = "" if c.is_usable() else " (NOT USABLE — too few samples)"
    return (
        f"AdvisorCalibration v{c.version}{usable}\n"
        f"  n_sessions:           {c.n_sessions}\n"
        f"  extracted_from:       {c.extracted_from}\n"
        f"  m13_score (n={c.m13_score_n}):\n"
        f"    p10 = {c.m13_score_p10:.3f}  ← gate trigger\n"
        f"    p25 = {c.m13_score_p25:.3f}\n"
        f"    p50 = {c.m13_score_p50:.3f}\n"
        f"  topic_drift (n={c.topic_drift_n}):\n"
        f"    p50 = {c.topic_drift_p50:.3f}\n"
        f"    p75 = {c.topic_drift_p75:.3f}\n"
        f"    p90 = {c.topic_drift_p90:.3f}\n"
        f"    p95 = {c.topic_drift_p95:.3f}  ← gate trigger\n"
    )


__all__ = [
    "AdvisorCalibration",
    "MIN_SAMPLES_FOR_CALIBRATION",
    "calibration_from_dict",
    "calibration_to_dict",
    "default_calibration",
    "default_calibration_path",
    "extract_calibration_from_audit",
    "load_calibration",
    "load_calibration_or_default",
    "render_calibration",
    "save_calibration",
]
