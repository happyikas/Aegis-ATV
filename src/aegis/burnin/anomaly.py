"""Burn-in distribution baseline + anomaly comparator (PR-ε).

The patent design intent (CCTV / 정상 범위 비교) requires a *baseline
distribution* of agent behaviour built from burn-in data. Each new
session is then compared against that baseline along time + I/O
axes: token-velocity z-score, cache_hit_rate vs distribution,
backtrack count percentile, etc. Anomalies surface as tags injected
into the temporal narrative (PR-θ).

This module provides:

* :class:`MetricStats` — μ/σ/p50/p95/p99/n per metric.
* :class:`BurnInBaseline` — collection of MetricStats keyed by
  metric name, plus provenance.
* :func:`extract_baseline_from_audit` — walks an audit chain
  (Stop retrospectives + the temporal-window aggregates from
  PR-θ) and computes baseline statistics. Honest about
  insufficient sample sizes — refuses to claim a tight
  distribution from < 5 sessions.
* :func:`compute_anomalies` — given a current
  :class:`TemporalContext` (PR-θ) + a baseline, compute per-
  metric z-scores and surface those exceeding configurable
  thresholds as :class:`AnomalyTag` objects.
* :func:`load_baseline` / :func:`save_baseline` — JSON I/O.
* Default baseline location: ``~/.aegis/burnin_baseline.json``
  with override via ``AEGIS_BURNIN_BASELINE_PATH``.

Privacy
-------
The baseline carries only aggregate statistics — no per-session
identifiers, no raw content, no token-level data. The audit
extractor reads only Stop ``session_retrospective`` records,
which are themselves pure metadata (PR #46 design).

What this is NOT
----------------
* Not a learned model — pure statistical thresholding (μ/σ)
* Not the M13 attribution head — that runs in parallel and
  serves a different purpose (per-call risk score)
* Not a full KS-test (yet) — z-scores against per-metric μ/σ
  catch the most common anomalies. Trajectory-shape KS-test is
  left for PR-η when we add a learned trajectory encoder.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

# Default thresholds for anomaly severity (per |z|).
INFO_THRESHOLD: float = 1.0
WARNING_THRESHOLD: float = 2.0
ALERT_THRESHOLD: float = 3.0

# Minimum number of sessions before the extractor will produce a
# baseline. Below this, σ estimates are unreliable.
MIN_SAMPLES_FOR_BASELINE: int = 5

# Default path lookup chain for the shipped baseline.
_DEFAULT_BASELINE_FILENAME = "burnin_baseline.json"


Severity = Literal["info", "warning", "alert"]


# ──────────────────────────────────────────────────────────────────────
# Statistics primitives
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetricStats:
    """Aggregate statistics for one metric across N burn-in samples.

    ``std`` of zero means the metric was constant in the burn-in
    set — z-score is then undefined and reported as 0 by callers.
    """

    mean: float
    std: float
    p50: float
    p95: float
    p99: float
    n_samples: int

    def z_score(self, observed: float) -> float:
        """Standard score. Returns 0 for degenerate (std≈0) baselines
        rather than infinity so callers can use the result safely."""
        if self.std < 1e-9 or self.n_samples < 2:
            return 0.0
        return float((observed - self.mean) / self.std)


def _compute_stats(values: list[float]) -> MetricStats:
    """Build :class:`MetricStats` from a flat list of samples."""
    if not values:
        return MetricStats(0.0, 0.0, 0.0, 0.0, 0.0, 0)
    n = len(values)
    mean = sum(values) / n
    if n >= 2:
        var = sum((v - mean) ** 2 for v in values) / (n - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    sorted_vals = sorted(values)

    def _pct(p: float) -> float:
        if n == 1:
            return sorted_vals[0]
        # Linear interpolation percentile.
        idx = (n - 1) * p
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return sorted_vals[lo]
        frac = idx - lo
        return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac

    return MetricStats(
        mean=float(mean),
        std=float(std),
        p50=float(_pct(0.50)),
        p95=float(_pct(0.95)),
        p99=float(_pct(0.99)),
        n_samples=n,
    )


# ──────────────────────────────────────────────────────────────────────
# Baseline dataclass
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BurnInBaseline:
    """Statistical baseline for burn-in-trained "normal" agent behaviour.

    Metrics are split into:

    * **session_*** — derived from one Stop ``session_retrospective``
      per session. Useful for "is this whole session anomalous?"

    * **window_*** — derived from a TemporalContext window over each
      session (PR-θ). Useful for "is the recent N-turn slice
      anomalous?"

    Provenance fields document where the numbers came from, so an
    operator can reason about whether a stale baseline is responsible
    for false positives.
    """

    version: int
    n_sessions: int
    extracted_at_ns: int
    extracted_from: str

    # Session-level
    session_cache_hit_rate: MetricStats
    session_backtrack_ratio: MetricStats
    session_redundancy_ratio: MetricStats
    session_error_rate: MetricStats
    session_cumulative_tokens: MetricStats
    session_cumulative_billed_dollars: MetricStats

    # Window-level (TemporalContext aggregates from PR-θ)
    window_token_velocity_per_turn: MetricStats
    window_cache_hit_rate_max_drop_pp: MetricStats
    window_n_backtracks: MetricStats
    window_n_redundant: MetricStats
    window_n_errors: MetricStats

    # Optional metadata
    notes: str = ""

    def is_usable(self) -> bool:
        """A baseline with too few samples produces unreliable
        z-scores. Comparator falls back to no-tags when this is
        ``False``."""
        return self.n_sessions >= MIN_SAMPLES_FOR_BASELINE


# ──────────────────────────────────────────────────────────────────────
# Anomaly tags
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AnomalyTag:
    """One observation that deviates from baseline by ≥ threshold σ."""

    metric: str
    severity: Severity
    observed: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    description: str

    def render_line(self) -> str:
        """One-line render for narrative injection."""
        sigil = {"info": "·", "warning": "⚠", "alert": "🚨"}[self.severity]
        return f"  {sigil} [{self.severity}] {self.description}"


def _severity_for(z: float) -> Severity | None:
    abs_z = abs(z)
    if abs_z >= ALERT_THRESHOLD:
        return "alert"
    if abs_z >= WARNING_THRESHOLD:
        return "warning"
    if abs_z >= INFO_THRESHOLD:
        return "info"
    return None


def _maybe_tag(
    metric: str,
    observed: float,
    stats: MetricStats,
    *,
    description_template: str,
) -> AnomalyTag | None:
    """Build a tag when the observed value deviates ≥ INFO_THRESHOLD σ
    from the baseline mean. Returns ``None`` otherwise."""
    if stats.n_samples < 2:
        return None
    z = stats.z_score(observed)
    sev = _severity_for(z)
    if sev is None:
        return None
    direction = "above" if z > 0 else "below"
    description = description_template.format(
        observed=observed,
        mean=stats.mean,
        std=stats.std,
        abs_z=abs(z),
        direction=direction,
    )
    return AnomalyTag(
        metric=metric,
        severity=sev,
        observed=observed,
        baseline_mean=stats.mean,
        baseline_std=stats.std,
        z_score=z,
        description=description,
    )


# ──────────────────────────────────────────────────────────────────────
# Comparator
# ──────────────────────────────────────────────────────────────────────


def compute_anomalies(
    *,
    temporal_ctx: Any,                     # aegis.atv.temporal.TemporalContext
    baseline: BurnInBaseline,
    session_retrospective: dict[str, Any] | None = None,
) -> list[AnomalyTag]:
    """Compare a TemporalContext (and optionally a session
    retrospective) against the baseline. Returns tags only for
    metrics that exceed INFO_THRESHOLD σ.

    ``session_retrospective`` is the dict from a Stop hook's
    ``explain.session_retrospective`` block (PR #46 shape). Pass
    it to also run session-level checks.
    """
    if not baseline.is_usable():
        return []

    tags: list[AnomalyTag] = []

    # Window-level metrics from TemporalContext
    if temporal_ctx is not None and len(temporal_ctx.history) > 0:
        observations: list[tuple[str, float, MetricStats, str]] = [
            (
                "window_token_velocity_per_turn",
                float(temporal_ctx.token_velocity_per_turn),
                baseline.window_token_velocity_per_turn,
                "token_velocity {observed:,.0f}/turn is {abs_z:.1f}σ "
                "{direction} burn-in baseline (μ={mean:,.0f}, σ={std:,.0f})",
            ),
            (
                "window_cache_hit_rate_max_drop_pp",
                float(temporal_ctx.cache_hit_rate_max_drop_pp),
                baseline.window_cache_hit_rate_max_drop_pp,
                "cache_hit_rate dropped {observed:.1f} pp within window "
                "({abs_z:.1f}σ {direction} normal drop μ={mean:.1f} pp)",
            ),
            (
                "window_n_backtracks",
                float(temporal_ctx.n_backtracks),
                baseline.window_n_backtracks,
                "{observed:.0f} backtracks in window — "
                "{abs_z:.1f}σ {direction} baseline μ={mean:.2f}",
            ),
            (
                "window_n_redundant",
                float(temporal_ctx.n_redundant),
                baseline.window_n_redundant,
                "{observed:.0f} redundant calls in window — "
                "{abs_z:.1f}σ {direction} baseline μ={mean:.2f}",
            ),
            (
                "window_n_errors",
                float(temporal_ctx.n_errors),
                baseline.window_n_errors,
                "{observed:.0f} tool errors in window — "
                "{abs_z:.1f}σ {direction} baseline μ={mean:.2f}",
            ),
        ]
        for metric, observed, stats, tmpl in observations:
            tag = _maybe_tag(metric, observed, stats, description_template=tmpl)
            if tag is not None:
                tags.append(tag)

    # Session-level metrics from the Stop retrospective dict
    if session_retrospective is not None:
        retro_observations: list[tuple[str, str, MetricStats, str]] = [
            (
                "session_cache_hit_rate",
                "cache_hit_rate",
                baseline.session_cache_hit_rate,
                "session cache_hit_rate {observed:.1%} is "
                "{abs_z:.1f}σ {direction} burn-in μ={mean:.1%}",
            ),
            (
                "session_backtrack_ratio",
                "backtrack_ratio",
                baseline.session_backtrack_ratio,
                "backtrack_ratio {observed:.2f} is "
                "{abs_z:.1f}σ {direction} burn-in μ={mean:.2f}",
            ),
            (
                "session_redundancy_ratio",
                "redundancy_ratio",
                baseline.session_redundancy_ratio,
                "redundancy_ratio {observed:.2f} is "
                "{abs_z:.1f}σ {direction} burn-in μ={mean:.2f}",
            ),
            (
                "session_error_rate",
                "error_rate",
                baseline.session_error_rate,
                "error_rate {observed:.2f} is "
                "{abs_z:.1f}σ {direction} burn-in μ={mean:.2f}",
            ),
            (
                "session_cumulative_tokens",
                "input_tokens_total",        # proxy
                baseline.session_cumulative_tokens,
                "session input tokens {observed:,.0f} is "
                "{abs_z:.1f}σ {direction} burn-in μ={mean:,.0f}",
            ),
            (
                "session_cumulative_billed_dollars",
                "cumulative_billed_dollars",
                baseline.session_cumulative_billed_dollars,
                "session billed ${observed:.4f} is "
                "{abs_z:.1f}σ {direction} burn-in μ=${mean:.4f}",
            ),
        ]
        for metric, key, stats, tmpl in retro_observations:
            value = session_retrospective.get(key)
            if value is None:
                continue
            tag = _maybe_tag(
                metric, float(value), stats, description_template=tmpl,
            )
            if tag is not None:
                tags.append(tag)

    # Sort: alerts first, then warnings, then info — UX priority.
    sev_order = {"alert": 0, "warning": 1, "info": 2}
    tags.sort(key=lambda t: (sev_order[t.severity], t.metric))
    return tags


# ──────────────────────────────────────────────────────────────────────
# Audit-chain extractor
# ──────────────────────────────────────────────────────────────────────


def _stream_jsonl(path: Path) -> Iterator[dict[str, Any]]:
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


def default_baseline() -> BurnInBaseline:
    """Synthetic-but-reasonable burn-in baseline for shipping.

    The numbers below are derived from observed Claude Code agent
    behaviour on a typical Solo Free deployment (low spending,
    short tool sequences, occasional backtracks). They are NOT
    user-specific and do NOT leak any operator's audit data.

    Operators with substantial real history should run
    :func:`extract_baseline_from_audit` to get a personalised
    baseline. Until they do, ``default_baseline()`` provides
    sensible thresholds — better to surface 1 in 100 false
    positives than zero anomaly tags.
    """
    return BurnInBaseline(
        version=1,
        n_sessions=100,                          # synthetic sample size
        extracted_at_ns=0,
        extracted_from="synthetic-default",
        # Session-level (typical Claude Code session)
        session_cache_hit_rate=MetricStats(
            mean=0.40, std=0.30, p50=0.40, p95=0.85, p99=0.95,
            n_samples=100,
        ),
        session_backtrack_ratio=MetricStats(
            mean=0.05, std=0.05, p50=0.04, p95=0.15, p99=0.25,
            n_samples=100,
        ),
        session_redundancy_ratio=MetricStats(
            mean=0.03, std=0.04, p50=0.02, p95=0.10, p99=0.20,
            n_samples=100,
        ),
        session_error_rate=MetricStats(
            mean=0.10, std=0.08, p50=0.08, p95=0.25, p99=0.40,
            n_samples=100,
        ),
        session_cumulative_tokens=MetricStats(
            mean=80_000, std=60_000, p50=60_000,
            p95=200_000, p99=350_000, n_samples=100,
        ),
        session_cumulative_billed_dollars=MetricStats(
            mean=1.50, std=2.00, p50=0.80,
            p95=6.00, p99=12.00, n_samples=100,
        ),
        # Window-level (5-turn slice)
        window_token_velocity_per_turn=MetricStats(
            mean=800, std=600, p50=600, p95=2_000, p99=3_500,
            n_samples=100,
        ),
        window_cache_hit_rate_max_drop_pp=MetricStats(
            mean=8.0, std=12.0, p50=3.0, p95=35.0, p99=55.0,
            n_samples=100,
        ),
        window_n_backtracks=MetricStats(
            mean=0.10, std=0.30, p50=0.0, p95=1.0, p99=2.0,
            n_samples=100,
        ),
        window_n_redundant=MetricStats(
            mean=0.20, std=0.40, p50=0.0, p95=1.0, p99=2.0,
            n_samples=100,
        ),
        window_n_errors=MetricStats(
            mean=0.20, std=0.40, p50=0.0, p95=1.0, p99=2.0,
            n_samples=100,
        ),
        notes=(
            "Synthetic defaults. Replace with extract_baseline_from_audit() "
            "output once your audit chain has 50+ sessions of Stop "
            "retrospective data."
        ),
    )


def extract_baseline_from_audit(
    audit_path: Path,
    *,
    notes: str = "",
) -> BurnInBaseline:
    """Walk an audit JSONL, aggregate Stop ``session_retrospective``
    records into a :class:`BurnInBaseline`.

    Window-level metrics (TemporalContext aggregates) are NOT
    derivable from the audit alone — they need transcript pairing
    (PR-θ). For those metrics we use placeholder stats with
    ``n_samples=0``, signalling the comparator to skip them.

    A user that wants window-level baselines can compute them
    separately and merge — left as a follow-up if/when the audit
    chain starts persisting per-window aggregates.
    """
    retros: list[dict[str, Any]] = []
    for rec in _stream_jsonl(audit_path):
        if rec.get("hook") != "Stop":
            continue
        retro = (rec.get("explain") or {}).get("session_retrospective")
        if isinstance(retro, dict):
            retros.append(retro)

    def col(key: str) -> list[float]:
        out: list[float] = []
        for r in retros:
            v = r.get(key)
            if v is None:
                continue
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return out

    return BurnInBaseline(
        version=1,
        n_sessions=len(retros),
        extracted_at_ns=time.time_ns(),
        extracted_from=f"audit:{audit_path}",
        session_cache_hit_rate=_compute_stats(col("cache_hit_rate")),
        session_backtrack_ratio=_compute_stats(col("backtrack_ratio")),
        session_redundancy_ratio=_compute_stats(col("redundancy_ratio")),
        session_error_rate=_compute_stats(col("error_rate")),
        session_cumulative_tokens=_compute_stats(col("input_tokens_total")),
        session_cumulative_billed_dollars=_compute_stats(
            col("cumulative_billed_dollars")
        ),
        # Window-level not extractable from audit alone — placeholders.
        window_token_velocity_per_turn=_compute_stats([]),
        window_cache_hit_rate_max_drop_pp=_compute_stats([]),
        window_n_backtracks=_compute_stats([]),
        window_n_redundant=_compute_stats([]),
        window_n_errors=_compute_stats([]),
        notes=notes,
    )


# ──────────────────────────────────────────────────────────────────────
# JSON I/O
# ──────────────────────────────────────────────────────────────────────


def baseline_to_dict(b: BurnInBaseline) -> dict[str, Any]:
    """JSON-serialisable form of a baseline. Stable schema for
    cross-version compatibility."""
    return asdict(b)


def baseline_from_dict(d: dict[str, Any]) -> BurnInBaseline:
    """Inverse of :func:`baseline_to_dict`. Handles missing/extra
    keys gracefully — defaults to empty stats so older baseline files
    keep loading after we add new metrics."""
    def _stats(key: str) -> MetricStats:
        s = d.get(key) or {}
        return MetricStats(
            mean=float(s.get("mean", 0.0)),
            std=float(s.get("std", 0.0)),
            p50=float(s.get("p50", 0.0)),
            p95=float(s.get("p95", 0.0)),
            p99=float(s.get("p99", 0.0)),
            n_samples=int(s.get("n_samples", 0)),
        )

    return BurnInBaseline(
        version=int(d.get("version", 1)),
        n_sessions=int(d.get("n_sessions", 0)),
        extracted_at_ns=int(d.get("extracted_at_ns", 0)),
        extracted_from=str(d.get("extracted_from", "")),
        session_cache_hit_rate=_stats("session_cache_hit_rate"),
        session_backtrack_ratio=_stats("session_backtrack_ratio"),
        session_redundancy_ratio=_stats("session_redundancy_ratio"),
        session_error_rate=_stats("session_error_rate"),
        session_cumulative_tokens=_stats("session_cumulative_tokens"),
        session_cumulative_billed_dollars=_stats(
            "session_cumulative_billed_dollars"
        ),
        window_token_velocity_per_turn=_stats(
            "window_token_velocity_per_turn"
        ),
        window_cache_hit_rate_max_drop_pp=_stats(
            "window_cache_hit_rate_max_drop_pp"
        ),
        window_n_backtracks=_stats("window_n_backtracks"),
        window_n_redundant=_stats("window_n_redundant"),
        window_n_errors=_stats("window_n_errors"),
        notes=str(d.get("notes", "")),
    )


def save_baseline(baseline: BurnInBaseline, path: Path) -> None:
    """Write JSON, mode 0644. Creates parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(baseline_to_dict(baseline), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_baseline(path: Path) -> BurnInBaseline:
    """Inverse of :func:`save_baseline`. Raises FileNotFoundError when
    the path is absent — use :func:`load_baseline_or_default` for the
    "may not exist" path."""
    if not path.is_file():
        raise FileNotFoundError(f"burn-in baseline not at {path}")
    return baseline_from_dict(
        json.loads(path.read_text(encoding="utf-8"))
    )


def default_baseline_path() -> Path:
    """Honours ``AEGIS_BURNIN_BASELINE_PATH`` env override; falls
    back to ``~/.aegis/burnin_baseline.json``."""
    override = os.environ.get("AEGIS_BURNIN_BASELINE_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aegis" / _DEFAULT_BASELINE_FILENAME


def load_baseline_or_default(
    path: Path | None = None,
) -> BurnInBaseline | None:
    """Best-effort load — returns ``None`` (rather than raising)
    when no baseline is configured yet. The narrative will then
    omit the ANOMALIES section, falling back gracefully."""
    p = path or default_baseline_path()
    try:
        return load_baseline(p)
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return None


# ──────────────────────────────────────────────────────────────────────
# Narrative renderer
# ──────────────────────────────────────────────────────────────────────


def render_anomalies(tags: list[AnomalyTag]) -> str:
    """Render a list of tags as the ANOMALIES vs BURN-IN section
    of a narrative. Empty list → empty string (caller should skip
    the section header in that case)."""
    if not tags:
        return ""
    lines = [
        f"ANOMALIES vs BURN-IN ({len(tags)} found, "
        f"sorted alert→warning→info)"
    ]
    for tag in tags:
        lines.append(tag.render_line())
    return "\n".join(lines)


