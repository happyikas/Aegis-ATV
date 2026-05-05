"""Live fleet cost monitor — tail ``~/.aegis/audit.jsonl`` and fire
notifier on threshold crossings.

PR #37's :func:`aegis.cost.multi_agent.multi_agent_replay` is offline
simulation. This module provides the **live** counterpart: a small
daemon that watches the audit JSONL written by every PreToolUse hook
process, aggregates cumulative cost across all sessions in real time,
and invokes the same Notifier protocol when fleet cost crosses
operator-set thresholds.

Architecture (no IPC needed)
----------------------------

Every Claude Code hook process appends a line to the same
``~/.aegis/audit.jsonl`` (the SHA3 chain in ``aegis.audit.local_chain``).
We tail that file with offset bookkeeping (``~/.aegis/fleet_monitor.state``)
so the monitor can survive restarts without double-counting. No
sockets / pipes — file-tail is fine for single-host fleets and
trivially robust.

Fleet aggregation
-----------------

Per-aid cumulative cost is parsed out of each PreToolUse record's
step335 trace (``cum=X.XXXX``). We track the **max** cum per session
because the trace is the running session total — fleet cost is the
sum of those per-session maxes at any point in time.

Threshold semantics match :class:`aegis.cost.multi_agent.FleetThreshold`:
warn (default → continue), hard_stop (default → write a stop-flag file
that hook processes can poll). The daemon itself never touches
the running hook processes — it only emits notifications and an
optional stop-flag.

Lifecycle
---------

  $ aegis fleet-monitor start --threshold 5.0 --hard-stop 20.0 \
      --slack-url-env SLACK_WEBHOOK_URL
  $ aegis fleet-monitor status
  $ aegis fleet-monitor stop

The CLI lives in :mod:`tools.aegis_cli`. The daemon process polls
once every ``poll_interval_s`` (default 1.0 s). On crash, the next
``start`` resumes from the last saved offset.
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aegis.cost.multi_agent import (
    FleetCrossing,
    FleetThreshold,
    Notifier,
    StderrNotifier,
)

# step335 trace shape: "step335: ok (cum=0.0123, forecast=...)"
_CUM_RE = re.compile(r"cum=([\d.]+)")

DEFAULT_AUDIT_PATH: Path = Path.home() / ".aegis" / "audit.jsonl"
DEFAULT_STATE_PATH: Path = Path.home() / ".aegis" / "fleet_monitor.state"
DEFAULT_PID_PATH: Path = Path.home() / ".aegis" / "fleet_monitor.pid"
DEFAULT_STOP_FLAG: Path = Path.home() / ".aegis" / "fleet_monitor.stop"
DEFAULT_POLL_INTERVAL_S: float = 1.0


@dataclass
class FleetMonitorState:
    """Persisted across daemon restarts so we don't double-count."""

    last_offset: int = 0
    per_aid_max_cum: dict[str, float] = field(default_factory=dict)
    fired_thresholds: list[float] = field(default_factory=list)
    fleet_dollars: float = 0.0
    n_records_seen: int = 0
    last_seen_ts_ns: int = 0


def _load_state(path: Path) -> FleetMonitorState:
    if not path.is_file():
        return FleetMonitorState()
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return FleetMonitorState(
            last_offset=int(d.get("last_offset", 0)),
            per_aid_max_cum=dict(d.get("per_aid_max_cum", {}) or {}),
            fired_thresholds=list(d.get("fired_thresholds", []) or []),
            fleet_dollars=float(d.get("fleet_dollars", 0.0)),
            n_records_seen=int(d.get("n_records_seen", 0)),
            last_seen_ts_ns=int(d.get("last_seen_ts_ns", 0)),
        )
    except (json.JSONDecodeError, ValueError, OSError):
        # Corrupt / unreadable state — better to start fresh than
        # silently misreport. Hook keeps running, monitor just
        # rebuilds from offset 0.
        return FleetMonitorState()


def _save_state(path: Path, state: FleetMonitorState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    tmp.replace(path)


def _parse_cum_dollars(record: dict[str, object]) -> float | None:
    """Pull ``cum=X`` out of step335 trace, or recover from the
    'cumulative_dollars X > budget' reason on REQUIRE_APPROVAL records."""
    explain = record.get("explain")
    if isinstance(explain, dict):
        traces = explain.get("step_traces")
        if isinstance(traces, dict):
            s335 = traces.get("aegis.firewall.step335_cost.run")
            if isinstance(s335, str):
                m = _CUM_RE.search(s335)
                if m:
                    try:
                        return float(m.group(1))
                    except ValueError:
                        return None
    # Fallback: parse from the reason string on overrun records.
    reason = record.get("reason")
    if isinstance(reason, str):
        m = re.search(r"cumulative_dollars\s+([\d.]+)\s+>\s+budget", reason)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


def process_new_records(
    *,
    audit_path: Path,
    state: FleetMonitorState,
    thresholds: list[FleetThreshold],
    notifier: Notifier,
    stop_flag: Path | None = None,
) -> int:
    """Read every PreToolUse record after ``state.last_offset``,
    update ``state.fleet_dollars``, fire ``notifier`` on crossings.

    Returns the number of NEW records processed. Idempotent — calling
    twice in a row with the same audit file processes 0 the second
    time.
    """
    if not audit_path.is_file():
        return 0
    n_new = 0
    fired: set[float] = set(state.fired_thresholds)
    threshold_tuple = tuple(sorted(thresholds, key=lambda t: t.dollars))

    with audit_path.open("rb") as fh:
        fh.seek(state.last_offset)
        for raw_line in fh:
            try:
                rec = json.loads(raw_line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            state.last_offset += len(raw_line)
            # Skip PostToolUse and other non-decision records.
            if "decision" not in rec:
                continue
            n_new += 1
            state.n_records_seen += 1
            ts_ns = int(rec.get("ts_ns", 0) or 0)
            if ts_ns > state.last_seen_ts_ns:
                state.last_seen_ts_ns = ts_ns
            aid = str(rec.get("aid", "unknown"))
            cum = _parse_cum_dollars(rec)
            if cum is None:
                continue

            # Update per-session max so fleet aggregation stays
            # monotone even if records arrive out of order (rare but
            # possible across rotations).
            prev = state.per_aid_max_cum.get(aid, 0.0)
            if cum <= prev:
                continue
            state.per_aid_max_cum[aid] = cum
            before = state.fleet_dollars
            state.fleet_dollars += (cum - prev)
            after = state.fleet_dollars

            # Fire any threshold the new fleet total just crossed.
            for t in threshold_tuple:
                if t.dollars in fired:
                    continue
                if before < t.dollars <= after:
                    decision = notifier.on_threshold_crossing(
                        threshold=t,
                        fleet_dollars=after,
                        aid=aid,
                        call_idx=state.n_records_seen,
                    )
                    fired.add(t.dollars)
                    state.fired_thresholds = sorted(fired)
                    if decision == "abort" and stop_flag is not None:
                        # Write a stop-flag so any hook process polling
                        # this path can refuse new tool calls.
                        try:
                            stop_flag.parent.mkdir(parents=True, exist_ok=True)
                            stop_flag.write_text(
                                json.dumps({
                                    "ts_ns": time.time_ns(),
                                    "threshold": asdict(t),
                                    "fleet_dollars": after,
                                    "aid_at_crossing": aid,
                                })
                            )
                        except OSError:
                            pass
    return n_new


def serve_forever(
    *,
    audit_path: Path,
    state_path: Path,
    pid_path: Path,
    stop_flag: Path,
    thresholds: list[FleetThreshold],
    notifier: Notifier,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> int:
    """Daemon main loop — polls the audit JSONL every
    ``poll_interval_s`` and fires the notifier. Exits cleanly on
    SIGTERM. Returns 0 on graceful shutdown."""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(json.dumps({"pid": os.getpid(), "ts_ns": time.time_ns()}))

    state = _load_state(state_path)
    stop_requested = {"flag": False}

    def _on_signal(signum: int, _frame: object) -> None:
        stop_requested["flag"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        while not stop_requested["flag"]:
            n = process_new_records(
                audit_path=audit_path,
                state=state,
                thresholds=thresholds,
                notifier=notifier,
                stop_flag=stop_flag,
            )
            if n > 0:
                _save_state(state_path, state)
            time.sleep(poll_interval_s)
        # Final flush.
        _save_state(state_path, state)
        return 0
    finally:
        import contextlib
        with contextlib.suppress(OSError):
            pid_path.unlink()


def is_running(pid_path: Path = DEFAULT_PID_PATH) -> bool:
    """True if the PID file exists and the process is alive."""
    if not pid_path.is_file():
        return False
    try:
        d = json.loads(pid_path.read_text())
        pid = int(d.get("pid", 0))
    except (json.JSONDecodeError, ValueError, OSError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def make_default_notifier(
    *,
    slack_webhook_url: str | None = None,
    ntfy_topic: str | None = None,
    ntfy_base_url: str = "https://ntfy.sh",
    crossings_log: Path | str | None = None,
    interactive: bool = False,
) -> Notifier:
    """Build the production notifier from CLI knobs.

    The notifier is always at least :class:`StderrNotifier`; every
    other channel (Slack / ntfy / file) is **added** to a
    :class:`CompositeNotifier` so operators can wire as many
    redundant channels as they want.

    Production combo (recommended, no Slack workspace required):

        make_default_notifier(
            ntfy_topic="aegis-cost-alerts-<uuid>",   # phone push
            crossings_log="~/.aegis/crossings.jsonl", # audit log
        )
    """
    notifiers: list[Notifier] = [StderrNotifier(interactive=interactive)]

    if slack_webhook_url:
        from aegis.cost.slack_notifier import SlackWebhookNotifier
        notifiers.append(SlackWebhookNotifier(slack_webhook_url))

    if ntfy_topic:
        from aegis.cost.ntfy_notifier import NtfyNotifier
        notifiers.append(NtfyNotifier(topic=ntfy_topic, base_url=ntfy_base_url))

    if crossings_log is not None:
        from aegis.cost.file_notifier import FileNotifier
        notifiers.append(FileNotifier(Path(crossings_log)))

    if len(notifiers) == 1:
        return notifiers[0]

    from aegis.cost.composite_notifier import CompositeNotifier
    return CompositeNotifier(notifiers)


__all__ = [
    "DEFAULT_AUDIT_PATH",
    "DEFAULT_PID_PATH",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_STATE_PATH",
    "DEFAULT_STOP_FLAG",
    "FleetCrossing",
    "FleetMonitorState",
    "FleetThreshold",
    "is_running",
    "make_default_notifier",
    "process_new_records",
    "serve_forever",
]
