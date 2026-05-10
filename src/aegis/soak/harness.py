"""Soak / load test orchestrator.

The harness is asyncio-based. One coroutine generates traffic at the
configured rate (token-bucket pacing — same algorithm the rate-limit
middleware uses, just for outbound shaping). N worker coroutines pull
requests off an unbounded queue and POST to the target sidecar.
A separate ticker coroutine does the periodic audit-chain
verification mid-soak.

Termination: when the configured duration elapses, the generator
stops enqueueing; workers drain. Final report is built from the
recorder's accumulated state.

Why this is in src/ (importable) rather than scripts/:

* tests can import ``run_soak`` directly with a stub httpx client
  so the harness logic itself is unit-testable;
* the CLI (``tools.aegis_cli``) wires :class:`SoakConfig` from the
  command line and calls ``run_soak`` — clean separation of CLI
  parsing vs. orchestration.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from aegis.soak.payloads import PAYLOAD_MIX, PayloadKind, payload_for

logger = logging.getLogger("aegis.soak")


# ──────────────────────────────────────────────────────────────────
# Config + thresholds
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SoakThresholds:
    """Pass / fail criteria. Hardcoded defaults match the production
    sign-off bar from the 3-month MVP review."""

    # Error rate cap. "Error" = HTTP 5xx, network failure, or
    # decision mismatch (firewall returned a different decision than
    # the payload expected).
    max_error_rate: float = 0.01           # 1%

    # Latency p99 cap (milliseconds). 500ms is comfortable headroom
    # over the steady-state ~12ms p50 from the existing benchmarks.
    max_p99_latency_ms: float = 500.0

    # Audit chain must verify clean at start, halfway, and end.
    require_clean_chain: bool = True

    # Throughput floor — completed requests / second over the soak
    # window. 0 disables. Useful for catching backpressure that
    # silently caps the harness below the configured rate.
    min_throughput_rps: float = 0.0


@dataclass(frozen=True)
class SoakConfig:
    """Inputs to :func:`run_soak`. All fields explicit so the CLI
    glue can render them at start (operator sees what they're
    signing off on)."""

    target_url: str                            # e.g. "http://localhost:8000"
    duration_s: float                          # total wall-clock time
    rate_per_s: float                          # outbound throttle
    concurrency: int = 16                      # worker count
    timeout_s: float = 5.0                     # per-request timeout
    seed: int = 42                             # RNG seed for reproducibility
    chain_verify_interval_s: float = 600.0     # 10 min default
    thresholds: SoakThresholds = field(default_factory=SoakThresholds)
    # Payload mix override. ``None`` → use :data:`PAYLOAD_MIX`.
    # Custom mixes for stress testing one path (e.g. all-BLOCK).
    payload_mix: tuple[Any, ...] | None = None


# ──────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────


@dataclass
class LatencyStats:
    """Sample-based latency stats. Bounded sample size keeps memory
    flat across a 24h soak — we keep a reservoir of ~10k samples,
    which gives p99 within ±0.5% of true under iid assumptions."""

    samples_ms: list[float] = field(default_factory=list)
    capacity: int = 10_000
    seen: int = 0

    def record(self, ms: float, rng: random.Random) -> None:
        self.seen += 1
        if len(self.samples_ms) < self.capacity:
            self.samples_ms.append(ms)
            return
        # Reservoir sampling: replace a random slot with probability
        # capacity/seen so the reservoir is always a uniform sample.
        idx = rng.randrange(self.seen)
        if idx < self.capacity:
            self.samples_ms[idx] = ms

    def percentile(self, p: float) -> float:
        if not self.samples_ms:
            return 0.0
        sorted_samples = sorted(self.samples_ms)
        k = max(0, min(len(sorted_samples) - 1, int(math.ceil(p * len(sorted_samples)) - 1)))
        return sorted_samples[k]


@dataclass
class SoakResult:
    """End-of-run snapshot. Returned by :func:`run_soak`; serialized
    to JSON for the CLI to dump."""

    started_at_ns: int
    ended_at_ns: int
    config: dict[str, Any]    # SoakConfig as a dict (for JSON)
    n_requested: int = 0      # total requests sent
    n_completed: int = 0      # 2xx + intentional 4xx
    n_errors: int = 0         # 5xx + network failure + decision mismatch
    decisions: dict[str, int] = field(
        default_factory=lambda: {"ALLOW": 0, "REQUIRE_APPROVAL": 0, "BLOCK": 0}
    )
    decision_mismatches: int = 0
    latency: LatencyStats = field(default_factory=LatencyStats)
    chain_checks: list[dict[str, Any]] = field(default_factory=list)
    # The final pass / fail per-criterion + overall.
    pass_overall: bool = False
    failures: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.ended_at_ns - self.started_at_ns) / 1_000_000_000

    @property
    def throughput_rps(self) -> float:
        d = self.duration_s
        return self.n_completed / d if d > 0 else 0.0

    @property
    def error_rate(self) -> float:
        if self.n_requested == 0:
            return 0.0
        return self.n_errors / self.n_requested

    def to_json(self) -> dict[str, Any]:
        return {
            "started_at_ns": self.started_at_ns,
            "ended_at_ns": self.ended_at_ns,
            "duration_s": round(self.duration_s, 3),
            "config": self.config,
            "n_requested": self.n_requested,
            "n_completed": self.n_completed,
            "n_errors": self.n_errors,
            "throughput_rps": round(self.throughput_rps, 3),
            "error_rate": round(self.error_rate, 5),
            "decisions": self.decisions,
            "decision_mismatches": self.decision_mismatches,
            "latency_ms": {
                "p50": round(self.latency.percentile(0.50), 2),
                "p95": round(self.latency.percentile(0.95), 2),
                "p99": round(self.latency.percentile(0.99), 2),
                "samples": len(self.latency.samples_ms),
                "total_seen": self.latency.seen,
            },
            "chain_checks": self.chain_checks,
            "pass_overall": self.pass_overall,
            "failures": self.failures,
        }


# ──────────────────────────────────────────────────────────────────
# The orchestrator
# ──────────────────────────────────────────────────────────────────


# Type alias for the request-sender. Tests inject a stub.
# Returns (status_code, decision_or_none, latency_ms, error_or_none).
RequestSender = Callable[
    [dict[str, Any]],
    Awaitable[tuple[int, str | None, float, str | None]],
]


def _evaluate_pass(
    result: SoakResult, thresholds: SoakThresholds,
) -> None:
    """Walk the thresholds and populate ``result.failures`` +
    ``result.pass_overall``."""
    failures: list[str] = []

    if result.error_rate > thresholds.max_error_rate:
        failures.append(
            f"error_rate {result.error_rate:.4f} > "
            f"max {thresholds.max_error_rate:.4f}"
        )

    p99 = result.latency.percentile(0.99)
    if p99 > thresholds.max_p99_latency_ms:
        failures.append(
            f"p99 latency {p99:.1f}ms > max {thresholds.max_p99_latency_ms:.1f}ms"
        )

    if (
        thresholds.min_throughput_rps > 0
        and result.throughput_rps < thresholds.min_throughput_rps
    ):
        failures.append(
            f"throughput {result.throughput_rps:.2f}/s < "
            f"min {thresholds.min_throughput_rps:.2f}/s"
        )

    if thresholds.require_clean_chain:
        for check in result.chain_checks:
            if not check.get("ok", False):
                failures.append(
                    f"chain verify failed at t={check.get('elapsed_s', '?')}s: "
                    f"{check.get('reason', 'unknown')}"
                )
                break

    result.failures = failures
    result.pass_overall = not failures


async def _http_request_sender(
    target_url: str, *, timeout_s: float,
) -> RequestSender:
    """Build the default httpx-backed request sender."""
    client = httpx.AsyncClient(
        base_url=target_url,
        timeout=timeout_s,
        # Keep-alive — soak generators reuse connections aggressively.
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
    )

    async def _send(payload: dict[str, Any]) -> tuple[int, str | None, float, str | None]:
        t0 = time.monotonic()
        try:
            resp = await client.post("/evaluate", json=payload)
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            try:
                body = resp.json()
            except ValueError:
                body = {}
            decision = body.get("decision") if isinstance(body, dict) else None
            return resp.status_code, decision, elapsed_ms, None
        except httpx.HTTPError as e:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            return 0, None, elapsed_ms, repr(e)

    # Stash the client on the sender so the orchestrator can close it.
    _send.__client__ = client  # type: ignore[attr-defined]
    return _send


async def _generator(
    config: SoakConfig,
    queue: asyncio.Queue[tuple[PayloadKind, dict[str, Any], str]],
    rng: random.Random,
    stop_at: float,
) -> int:
    """Token-bucket-paced payload generator. Returns total enqueued."""
    interval = 1.0 / max(0.001, config.rate_per_s)
    next_emit = time.monotonic()
    enqueued = 0
    mix = config.payload_mix or PAYLOAD_MIX
    while time.monotonic() < stop_at:
        kind, body, expected = payload_for(rng.random(), mix=mix)
        await queue.put((kind, body, expected))
        enqueued += 1
        next_emit += interval
        sleep_s = next_emit - time.monotonic()
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        else:
            # Rate is faster than we can keep up. Don't accumulate
            # debt — reset the next-emit anchor so we don't blast
            # the queue once we catch up.
            next_emit = time.monotonic()
    return enqueued


async def _worker(
    queue: asyncio.Queue[tuple[PayloadKind, dict[str, Any], str]],
    sender: RequestSender,
    result: SoakResult,
    rng: random.Random,
    stop_event: asyncio.Event,
) -> None:
    """Pull payload, send, record. Exits when queue is empty AND
    stop_event is set."""
    while True:
        try:
            kind, body, expected = await asyncio.wait_for(queue.get(), timeout=0.5)
        except TimeoutError:
            if stop_event.is_set() and queue.empty():
                return
            continue

        result.n_requested += 1
        status, decision, elapsed_ms, err = await sender(body)
        result.latency.record(elapsed_ms, rng)
        if err is not None:
            result.n_errors += 1
            queue.task_done()
            continue
        if status >= 500:
            result.n_errors += 1
            queue.task_done()
            continue
        # 2xx + intentional 4xx (e.g. 422 for malformed test payload)
        # both count as "completed" — the firewall produced a verdict.
        result.n_completed += 1
        if decision in result.decisions:
            result.decisions[decision] += 1
        if decision and decision != expected:
            result.decision_mismatches += 1
            # A decision mismatch is a regression; promote to error.
            result.n_errors += 1
        queue.task_done()


async def _chain_verifier(
    config: SoakConfig,
    result: SoakResult,
    stop_event: asyncio.Event,
    verify_fn: Callable[[], Awaitable[tuple[bool, str]]] | None,
) -> None:
    """Periodic audit-chain check. Records each outcome to
    ``result.chain_checks``. Skipped entirely when ``verify_fn`` is
    None (e.g. unit tests with no real audit log)."""
    if verify_fn is None:
        return
    interval = max(1.0, config.chain_verify_interval_s)
    started = time.monotonic()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break  # stop_event fired during the wait
        except TimeoutError:
            pass
        ok, reason = await verify_fn()
        result.chain_checks.append({
            "elapsed_s": round(time.monotonic() - started, 1),
            "ok": ok,
            "reason": reason,
        })


async def run_soak(
    config: SoakConfig,
    *,
    sender: RequestSender | None = None,
    chain_verify_fn: Callable[[], Awaitable[tuple[bool, str]]] | None = None,
    log_progress_fn: Callable[[SoakResult], None] | None = None,
) -> SoakResult:
    """Run the soak. Defaults to httpx against ``config.target_url``;
    tests inject a stub ``sender`` that doesn't open a network socket.

    ``chain_verify_fn``: optional async callable that returns
    ``(ok, reason)`` — typically wraps the existing
    :func:`aegis.audit.local_chain.verify_chain`. The harness runs it
    every ``config.chain_verify_interval_s`` and at termination.

    ``log_progress_fn``: optional callable invoked once per ~10s with
    the running ``SoakResult`` so the CLI can print live progress.
    """
    rng = random.Random(config.seed)

    # Build sender.
    if sender is None:
        sender = await _http_request_sender(
            config.target_url, timeout_s=config.timeout_s,
        )
    own_client = getattr(sender, "__client__", None)

    queue: asyncio.Queue[tuple[PayloadKind, dict[str, Any], str]] = asyncio.Queue(
        maxsize=max(1024, config.concurrency * 8),
    )
    started_at_ns = time.time_ns()
    stop_at_monotonic = time.monotonic() + config.duration_s

    result = SoakResult(
        started_at_ns=started_at_ns,
        ended_at_ns=started_at_ns,    # filled at end
        config={
            "target_url": config.target_url,
            "duration_s": config.duration_s,
            "rate_per_s": config.rate_per_s,
            "concurrency": config.concurrency,
            "timeout_s": config.timeout_s,
            "seed": config.seed,
            "chain_verify_interval_s": config.chain_verify_interval_s,
            "thresholds": {
                "max_error_rate": config.thresholds.max_error_rate,
                "max_p99_latency_ms": config.thresholds.max_p99_latency_ms,
                "require_clean_chain": config.thresholds.require_clean_chain,
                "min_throughput_rps": config.thresholds.min_throughput_rps,
            },
        },
    )

    stop_event = asyncio.Event()

    # Optional periodic progress logger — runs in parallel.
    async def _progress_loop() -> None:
        if log_progress_fn is None:
            return
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                break
            except TimeoutError:
                pass
            log_progress_fn(result)

    workers = [
        asyncio.create_task(
            _worker(queue, sender, result, rng, stop_event),
        )
        for _ in range(max(1, config.concurrency))
    ]
    verifier = asyncio.create_task(
        _chain_verifier(config, result, stop_event, chain_verify_fn),
    )
    progress_task = asyncio.create_task(_progress_loop())

    try:
        await _generator(config, queue, rng, stop_at_monotonic)
    finally:
        # Tell workers + verifier + progress loop to wind down.
        stop_event.set()

        # Wait for queue drain so we count every in-flight request.
        await queue.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        # One final chain check at termination (regardless of timer).
        if chain_verify_fn is not None:
            ok, reason = await chain_verify_fn()
            result.chain_checks.append({
                "elapsed_s": round(time.monotonic() - (stop_at_monotonic - config.duration_s), 1),
                "ok": ok,
                "reason": reason,
                "phase": "final",
            })

        verifier.cancel()
        progress_task.cancel()
        await asyncio.gather(verifier, progress_task, return_exceptions=True)

        if own_client is not None:
            # Defensive — closing an already-closed client raises;
            # we don't care.
            with contextlib.suppress(Exception):
                await own_client.aclose()

    result.ended_at_ns = time.time_ns()
    _evaluate_pass(result, config.thresholds)
    return result


def _format_result_human(result: SoakResult) -> str:
    """Pretty-print for the CLI."""
    lines = [
        f"soak result — {'PASS' if result.pass_overall else 'FAIL'}",
        f"  duration:        {result.duration_s:.1f}s",
        f"  requested:       {result.n_requested}",
        f"  completed:       {result.n_completed}",
        f"  errors:          {result.n_errors}  ({result.error_rate * 100:.2f}%)",
        f"  throughput:      {result.throughput_rps:.2f}/s",
        f"  latency p50:     {result.latency.percentile(0.50):.1f}ms",
        f"  latency p95:     {result.latency.percentile(0.95):.1f}ms",
        f"  latency p99:     {result.latency.percentile(0.99):.1f}ms",
        f"  decisions:       {dict(result.decisions)}",
        f"  decision miss:   {result.decision_mismatches}",
        f"  chain checks:    "
        f"{sum(1 for c in result.chain_checks if c.get('ok'))} passed / "
        f"{len(result.chain_checks)} total",
    ]
    if result.failures:
        lines.append("  failures:")
        for f in result.failures:
            lines.append(f"    - {f}")
    return "\n".join(lines)


def write_result_json(result: SoakResult, path: Any) -> None:
    """JSON dump for downstream tooling (CI gates, fleet-monitor).

    ``path`` is ``str | Path``; written atomically via temp-and-rename
    so a partial write doesn't leave a half-formed file.
    """
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(result.to_json(), indent=2, sort_keys=True))
    tmp.replace(p)
