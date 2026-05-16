"""ATMU auto-recovery — WAL replay on startup (v0.5.8, patent §5A).

Closes production gap #2 from the v0.5.6 self-audit. The ATMU
intent log records a state-machine row per tool call:

    TENTATIVE -> PREPARED -> COMMITTED  (happy path)
              \\-> ABORTED               (suppressed pre-release)
                  ROLLED_BACK            (post-commit reversal)
                  COMPENSATED            (external compensating action)
                  QUARANTINED            (circuit-breaker)

If the process crashes mid-flight, rows can be left in **non-terminal**
states (TENTATIVE or PREPARED) with no in-memory handle to advance
them. Without recovery, those rows sit in the WAL forever, polluting
``count_state(TxState.TENTATIVE)`` metrics, masking real anomalies,
and confusing forensic analysis.

This module sweeps non-terminal rows on startup and **transitions
them to ABORTED** with a structured "orphaned at startup" reason —
matching the safest interpretation of the patent claim: an
unfinished intent that nobody is actively driving must be presumed
not-released so downstream tooling can't assume side effects.

Policy
------

* **Sweep age threshold** (``max_age_hours``, default 24): rows
  younger than this are *not* swept — they may belong to a live
  concurrent process. Setting the threshold lower than the longest
  realistic tool call is the operator's responsibility.

* **From-state policy**: TENTATIVE and PREPARED rows are
  ALWAYS sweepable. Terminal states (COMMITTED, ABORTED, etc.) are
  ignored by definition.

* **Target state**: always ABORTED. We never auto-promote orphans
  to COMMITTED — that would assert a side effect we have no
  evidence of. The compensation plan (if any) is left attached so
  the operator can still run ``aegis rollback <trace>`` against
  the orphan.

Surface
-------

* `recover_orphans(intent_log, ...)` — programmatic API. Returns
  an `OrphanSweepResult` summarizing what was swept (or would have
  been swept, in `dry_run=True`).
* `aegis atmu recover [--dry-run] [--max-age-hours N]` — CLI.
* Sidecar startup calls `recover_orphans()` once during the FastAPI
  lifespan; local-mode hook does the same on its first invocation.

Safety properties
-----------------

* Idempotent — running the sweep twice produces no spurious work
  the second time (no rows left in non-terminal age-eligible
  state after the first run).
* Read-only when `dry_run=True` — fits the "operator preview"
  workflow that the rest of the v0.5 surface uses (`memory
  rotate --dry-run`, `memory claude-md` proposals without
  `--apply`).
* Failures isolated per row — a SQLite error on one transition
  doesn't abort the sweep. The result object reports `failed`
  separately from `swept`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from aegis.atmu.intent_log import IntentLog
from aegis.atmu.state_machine import TERMINAL_STATES, TxState

# Default sweep age — rows younger than this are presumed alive
# (a slow tool call still in flight). 24 h is generous; tune
# downwards with `--max-age-hours` for shorter-running tools.
DEFAULT_MAX_AGE_HOURS: float = 24.0

# Non-terminal states are exactly the ones recovery targets.
# `state_machine.TERMINAL_STATES` is the canonical source of truth.
NON_TERMINAL_STATES: frozenset[TxState] = frozenset(
    s for s in TxState if s not in TERMINAL_STATES
)


@dataclass(frozen=True)
class OrphanRecord:
    """One non-terminal row a sweep would (or did) touch."""

    record_id: str
    seq: int
    aid: str
    trace_id: str
    tool_name: str
    current_state: TxState
    age_seconds: float
    created_at_ns: int


@dataclass(frozen=True)
class OrphanSweepResult:
    """Outcome of one `recover_orphans()` invocation.

    Fields are sized so the caller can render a one-line summary
    (`{n_swept} swept, {n_skipped_young} too young to touch,
    {n_failed} failed`) without further bookkeeping.
    """

    swept: tuple[OrphanRecord, ...] = field(default_factory=tuple)
    skipped_young: tuple[OrphanRecord, ...] = field(default_factory=tuple)
    failed: tuple[tuple[OrphanRecord, str], ...] = field(default_factory=tuple)
    dry_run: bool = False
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS
    now_ns: int = 0

    @property
    def n_swept(self) -> int:
        return len(self.swept)

    @property
    def n_skipped_young(self) -> int:
        return len(self.skipped_young)

    @property
    def n_failed(self) -> int:
        return len(self.failed)

    @property
    def n_total_eligible(self) -> int:
        """Rows in a non-terminal state, regardless of age."""
        return self.n_swept + self.n_skipped_young + self.n_failed


def _list_non_terminal_records(
    intent_log: IntentLog,
) -> list[dict[str, Any]]:
    """Fetch every row currently in a non-terminal state.

    Uses the same connection as the IntentLog — no separate handle,
    no schema duplication. Returns the canonical dict shape that
    ``IntentLog.get()`` produces.
    """
    placeholders = ",".join("?" * len(NON_TERMINAL_STATES))
    rows = intent_log.conn.execute(
        f"SELECT record_id FROM intent_log "
        f"WHERE current_state IN ({placeholders}) "
        f"ORDER BY seq",
        tuple(s.value for s in NON_TERMINAL_STATES),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for (rid,) in rows:
        rec = intent_log.get(rid)
        if rec is not None:
            out.append(rec)
    return out


def _to_orphan(rec: dict[str, Any], now_ns: int) -> OrphanRecord:
    """Project a row dict into the public OrphanRecord shape."""
    created = int(rec.get("created_at_ns", rec.get("ts_ns", now_ns)))
    age_s = max(0.0, (now_ns - created) / 1_000_000_000.0)
    return OrphanRecord(
        record_id=str(rec["record_id"]),
        seq=int(rec["seq"]),
        aid=str(rec.get("aid", "")),
        trace_id=str(rec.get("trace_id", "")),
        tool_name=str(rec.get("tool_name", "")),
        current_state=TxState(str(rec["current_state"])),
        age_seconds=age_s,
        created_at_ns=created,
    )


def find_orphans(
    intent_log: IntentLog,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now_ns: int | None = None,
) -> tuple[tuple[OrphanRecord, ...], tuple[OrphanRecord, ...]]:
    """Return ``(eligible, too_young)`` — non-terminal rows split
    by age. Pure function — does not mutate the WAL. Used by
    `recover_orphans()` and by `aegis atmu recover --dry-run`.

    A row is *eligible* when ``age_seconds >= max_age_hours*3600``.
    Setting ``max_age_hours=0`` makes everything non-terminal
    eligible — useful for tests + operator overrides.
    """
    if now_ns is None:
        now_ns = time.time_ns()
    threshold_s = max_age_hours * 3600.0
    eligible: list[OrphanRecord] = []
    too_young: list[OrphanRecord] = []
    for rec in _list_non_terminal_records(intent_log):
        o = _to_orphan(rec, now_ns)
        if o.age_seconds >= threshold_s:
            eligible.append(o)
        else:
            too_young.append(o)
    return tuple(eligible), tuple(too_young)


def recover_orphans(
    intent_log: IntentLog,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    dry_run: bool = False,
    now_ns: int | None = None,
    reason: str = "orphaned at startup — auto-recovered (ATMU §5A)",
) -> OrphanSweepResult:
    """Sweep non-terminal rows older than ``max_age_hours``,
    transitioning each to ABORTED with a structured reason.

    Returns an ``OrphanSweepResult``. Idempotent — calling twice
    produces no spurious work (the second call sees only fresh
    rows that haven't yet aged past the threshold).

    With ``dry_run=True``: identifies what *would* be swept,
    transitions nothing.

    Per-row failures are isolated — a single ``InvalidTransition``
    or SQLite error doesn't abort the sweep. The failing row lands
    in ``result.failed`` with the exception text; the rest of the
    sweep continues.
    """
    if now_ns is None:
        now_ns = time.time_ns()
    eligible, too_young = find_orphans(
        intent_log,
        max_age_hours=max_age_hours,
        now_ns=now_ns,
    )

    if dry_run:
        return OrphanSweepResult(
            swept=eligible,        # represents "would-sweep"
            skipped_young=too_young,
            failed=(),
            dry_run=True,
            max_age_hours=max_age_hours,
            now_ns=now_ns,
        )

    swept: list[OrphanRecord] = []
    failed: list[tuple[OrphanRecord, str]] = []
    for o in eligible:
        try:
            intent_log.transition(
                o.record_id,
                new_state=TxState.ABORTED,
                reason=reason,
            )
            swept.append(o)
        except Exception as exc:  # noqa: BLE001 — defensive sweep
            failed.append((o, str(exc)))

    return OrphanSweepResult(
        swept=tuple(swept),
        skipped_young=too_young,
        failed=tuple(failed),
        dry_run=False,
        max_age_hours=max_age_hours,
        now_ns=now_ns,
    )


def render_sweep_summary(result: OrphanSweepResult) -> str:
    """One-paragraph plain-text summary for CLI / log output."""
    verb = "would sweep" if result.dry_run else "swept"
    lines = [
        f"ATMU orphan {verb}: {result.n_swept} "
        f"row(s); skipped {result.n_skipped_young} too young; "
        f"failed {result.n_failed}",
        f"  age threshold: {result.max_age_hours:.1f}h",
    ]
    if result.n_swept:
        lines.append(f"  {verb}:")
        for o in result.swept[:20]:
            lines.append(
                f"    seq={o.seq:>5}  state={o.current_state.value:<12}  "
                f"age={o.age_seconds / 3600.0:6.2f}h  "
                f"tool={o.tool_name}  aid={o.aid}"
            )
        if len(result.swept) > 20:
            lines.append(f"    … {len(result.swept) - 20} more")
    if result.n_failed:
        lines.append("  failed:")
        for o, err in result.failed[:10]:
            lines.append(f"    seq={o.seq} record={o.record_id}: {err}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MAX_AGE_HOURS",
    "NON_TERMINAL_STATES",
    "OrphanRecord",
    "OrphanSweepResult",
    "find_orphans",
    "recover_orphans",
    "render_sweep_summary",
]
