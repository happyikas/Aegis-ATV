"""Multi-endpoint scrape orchestration for the inference registry.

Builds on :mod:`aegis.inference.vllm_metrics` (single-endpoint scrape)
and :mod:`aegis.inference.registry` (multi-endpoint config). Walks
every enabled endpoint in a registry, scrapes the scrapeable ones in
parallel, and produces a per-aid result map where each entry is one
of:

* :class:`InferenceMetrics` — successful scrape
* :class:`EndpointUnreachable` — vllm endpoint that timed out or
  returned an error; the scrape result records ``endpoint_unreachable=1``
  so the consumer can distinguish "no metrics" from "metrics scraped
  but not yet warmed up"
* :class:`EndpointSkipped` — non-scrapeable endpoint (cloud / disabled)

The orchestrator never raises on a per-endpoint failure: a scrape is
a best-effort observation, not a security gate. A registry-level
error (TOML malformed) is separately surfaced via :class:`InferenceRegistryError`
at load time — this module only runs after a registry has been
validated.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from typing import Final

from aegis.inference.registry import EndpointConfig, InferenceRegistry
from aegis.inference.vllm_metrics import (
    InferenceMetrics,
    VLLMMetricsError,
    scrape_vllm_metrics,
)

# Cap parallelism so a 100-endpoint registry doesn't open 100 sockets
# at once. Most realistic deployments are ≤10, so this only kicks in
# for fleet operators.
_MAX_CONCURRENCY: Final[int] = 16


@dataclass(frozen=True)
class EndpointUnreachable:
    """A scrapeable endpoint that failed to respond.

    The ``reason`` is the human-readable error from
    :class:`VLLMMetricsError` — surfaced verbatim in dashboards so an
    operator can see whether it's a DNS issue, timeout, or HTTP 5xx.
    The cross-reference flag ``endpoint_unreachable=1`` is what
    ``aegis report --by-aid-and-provider --with-live`` reads.
    """

    aid: str
    metrics_url: str
    reason: str
    endpoint_unreachable: int = 1


@dataclass(frozen=True)
class EndpointSkipped:
    """Non-scrapeable endpoint (cloud provider, or ``enabled = false``).

    Recording these in the result map lets the consumer say "Agent C:
    cloud provider, no metrics" instead of silently dropping the row.
    """

    aid: str
    provider: str
    provider_name: str | None
    reason: str  # e.g. "cloud provider has no /metrics endpoint"


# Discriminated union: every aid in the registry gets one of these.
ScrapeResult = InferenceMetrics | EndpointUnreachable | EndpointSkipped


def _scrape_one(ep: EndpointConfig) -> ScrapeResult:
    """Scrape a single endpoint, converting any failure into an
    ``EndpointUnreachable`` so the worker pool doesn't propagate
    exceptions.

    The caller should NOT see :class:`VLLMMetricsError` from this
    function — best-effort scrape semantics demand that all errors
    become typed results.
    """
    if not ep.enabled:
        return EndpointSkipped(
            aid=ep.aid,
            provider=ep.provider,
            provider_name=ep.provider_name,
            reason="endpoint disabled in inference.toml",
        )
    if ep.provider != "vllm":
        # cloud / ollama / tgi — recorded for attribution but not
        # scraped by this code path. Adapter PRs can light up
        # ollama / tgi by adding their own scrape function and
        # dispatching here.
        return EndpointSkipped(
            aid=ep.aid,
            provider=ep.provider,
            provider_name=ep.provider_name,
            reason=(
                "cloud provider has no /metrics endpoint"
                if ep.provider == "cloud"
                else f"provider {ep.provider!r} adapter not yet shipped"
            ),
        )

    assert ep.metrics_url is not None  # is_scrapeable() guarantees this
    try:
        return scrape_vllm_metrics(
            ep.metrics_url,
            timeout_s=ep.timeout_s,
        )
    except VLLMMetricsError as e:
        return EndpointUnreachable(
            aid=ep.aid,
            metrics_url=ep.metrics_url,
            reason=str(e),
        )


def scrape_all(
    registry: InferenceRegistry,
    *,
    max_workers: int = _MAX_CONCURRENCY,
) -> dict[str, ScrapeResult]:
    """Scrape every endpoint in ``registry``, concurrently for the
    scrapeable ones.

    Args:
        registry: A loaded :class:`InferenceRegistry`. Empty registry
            is fine — returns an empty dict, not an error.
        max_workers: Pool size. Defaults to a small cap to avoid
            socket exhaustion on big fleets. The pool is sized to
            ``min(len(registry), max_workers)``.

    Returns:
        A mapping ``{aid: ScrapeResult}`` with one entry per endpoint
        in the registry, including disabled / cloud ones (recorded as
        :class:`EndpointSkipped`). Entries are not ordered — callers
        that want a stable order should sort by ``aid``.

    Never raises. A scrape that fails for any reason becomes an
    :class:`EndpointUnreachable` in the result map.
    """
    if registry.is_empty():
        return {}

    results: dict[str, ScrapeResult] = {}
    pool_size = max(1, min(len(registry.endpoints), max_workers))
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=pool_size,
        thread_name_prefix="aegis-scrape",
    ) as pool:
        # ThreadPoolExecutor.map preserves submission order; we use
        # submit()+as_completed so a slow endpoint doesn't block the
        # fast ones from being recorded.
        futures = {
            pool.submit(_scrape_one, ep): ep
            for ep in registry.endpoints
        }
        for fut in concurrent.futures.as_completed(futures):
            ep = futures[fut]
            # _scrape_one is exception-free by contract, but defensive
            # coding: if a future *somehow* raises, materialise it as
            # unreachable so a single bug doesn't kill the whole
            # scrape.
            try:
                results[ep.aid] = fut.result()
            except Exception as e:  # noqa: BLE001 — defensive
                results[ep.aid] = EndpointUnreachable(
                    aid=ep.aid,
                    metrics_url=ep.metrics_url or "",
                    reason=f"unexpected scraper exception: {e!r}",
                )

    return results


def kv_pressure_band(result: ScrapeResult) -> str:
    """Convenience: pull the KV-pressure band out of a result regardless
    of variant. Returns ``"n/a"`` for non-scraped variants so a
    dashboard column can render uniformly.
    """
    if isinstance(result, InferenceMetrics):
        return result.kv_cache_pressure_band()
    return "n/a"
