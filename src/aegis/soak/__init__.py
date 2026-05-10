"""Soak / load test harness for the Aegis sidecar.

Public surface: :func:`run_soak` (the orchestrator) +
:class:`SoakConfig` / :class:`SoakResult` data classes for the CLI
glue in ``tools.aegis_cli``.

The whole point of this module is to make the 24h-duration
production sign-off test reproducible. The user runs
``aegis soak --duration 24h --rate 10/s --target http://...`` against
a real deployment; the harness:

* generates realistic ``/evaluate`` POST traffic at the configured
  rate, mixed across ALLOW / REQUIRE_APPROVAL / BLOCK shapes (so we
  exercise every firewall path);
* records per-second + windowed metrics — throughput, p50 / p95 /
  p99 latency, error rate;
* periodically (every ~10 min during a long soak) hits the
  ``aegis verify-audit`` boundary by replaying the audit chain head
  and confirming it grew monotonically without breaks;
* on completion, evaluates pass / fail per a configurable threshold
  set and writes a JSON summary.

Design constraints:

* **No external dependencies beyond what's already in
  ``pyproject.toml``.** The harness uses ``httpx`` (already a dep)
  + asyncio. No locust, no k6 — those would be heavier to install
  in a production-customer environment.
* **Hermetic on small inputs.** A 5-minute "smoke" flag exists for
  CI: same harness, scaled-down, runs in 90s of test wall time.
* **Honest pass / fail.** Thresholds are explicit and configurable.
  The CLI prints them at start so the operator knows what they're
  signing off on.

What this harness DOESN'T do (out of scope, for the runbook):

* Memory / RSS / disk monitoring of the *target* process — the
  operator runs ``ps`` / ``du`` / their fleet monitor for that.
* Distributed load generation across multiple machines — single-
  machine harness, sufficient for the MVP soak target rate (~50/s).
* Failure-injection (chaos) — separate concern.
"""

from aegis.soak.harness import (
    LatencyStats,
    SoakConfig,
    SoakResult,
    SoakThresholds,
    _format_result_human,
    run_soak,
    write_result_json,
)
from aegis.soak.payloads import (
    PAYLOAD_MIX,
    PayloadKind,
    payload_for,
)

__all__ = [
    "PAYLOAD_MIX",
    "LatencyStats",
    "PayloadKind",
    "SoakConfig",
    "SoakResult",
    "SoakThresholds",
    "_format_result_human",
    "payload_for",
    "run_soak",
    "write_result_json",
]
