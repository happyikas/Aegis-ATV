"""Anthropic Admin API integration — pull real billing/usage data.

The cache-aware billing proxy (:mod:`aegis.cost.pricing`) is a model
of what Anthropic should charge based on token counts. The Admin API
returns what Anthropic ACTUALLY charged. For invoice reconciliation,
auditing, and the M12 cost-attestation forensic chain, this is the
ground truth.

API contract
------------

* Endpoint: ``GET /v1/organizations/usage_report/messages``
* Auth: ``x-api-key: <ANTHROPIC_ADMIN_KEY>`` (a SEPARATE key from
  the regular API key — Admin key has read-only access to billing
  metadata for the org).
* Query params:
    - ``starting_at`` (ISO 8601 datetime, inclusive)
    - ``ending_at`` (ISO 8601 datetime, exclusive)
    - ``limit`` (max page size, server caps at 1000)
    - ``page`` (cursor for pagination)
    - ``group_by[]`` (e.g. ``model``, ``api_key_id``, ``workspace_id``)
* Response: ``{"data": [{...}, ...], "next_page": "..."}``

Each datum carries:
    - ``starting_at`` / ``ending_at`` (interval boundaries)
    - ``model`` / ``service_tier``
    - ``input_tokens`` / ``output_tokens`` /
      ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
    - ``server_tool_use`` (web_fetch / web_search counts)
    - ``api_key_id`` / ``workspace_id`` if grouped

This module is a pure HTTP client over stdlib ``urllib.request`` —
no new project deps. Failure-isolated (timeout / 4xx / 5xx all
return a structured error rather than crash).

Reference: https://docs.anthropic.com/en/api/admin-api/usage-cost
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_BASE_URL: str = "https://api.anthropic.com"
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_USER_AGENT: str = "Aegis-ATV-cost-import/1.0"
ADMIN_API_VERSION: str = "2023-06-01"


@dataclass
class UsageRecord:
    """One row from the Admin API ``usage_report/messages`` endpoint.

    Fields mirror what Anthropic returns; we keep raw token counts
    so callers can apply :func:`aegis.cost.pricing.billed_dollars`
    consistently with the rest of the project.
    """

    starting_at: str            # ISO 8601
    ending_at: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    api_key_id: str | None = None
    workspace_id: str | None = None
    service_tier: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    """Aggregate of :func:`fetch`. Errors keep the partial data so a
    paginated 503 mid-walk doesn't lose the first 9 pages."""

    records: list[UsageRecord] = field(default_factory=list)
    pages_fetched: int = 0
    error: str | None = None
    requested_starting_at: str = ""
    requested_ending_at: str = ""


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _parse_since(spec: str) -> datetime:
    """Accept ``30d`` / ``24h`` / ISO-8601 ``2026-04-15T00:00:00Z``."""
    spec = (spec or "").strip()
    now = datetime.now(UTC).replace(tzinfo=None)
    if not spec:
        return now - timedelta(days=30)
    if spec.endswith("d"):
        return now - timedelta(days=int(spec[:-1]))
    if spec.endswith("h"):
        return now - timedelta(hours=int(spec[:-1]))
    # Try ISO 8601 — strip trailing Z for fromisoformat.
    return datetime.fromisoformat(spec.rstrip("Z"))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_from_payload(payload: dict[str, Any]) -> UsageRecord:
    return UsageRecord(
        starting_at=str(payload.get("starting_at", "")),
        ending_at=str(payload.get("ending_at", "")),
        model=str(payload.get("model", "")),
        input_tokens=int(payload.get("input_tokens", 0) or 0),
        output_tokens=int(payload.get("output_tokens", 0) or 0),
        cache_creation_input_tokens=int(
            payload.get("cache_creation_input_tokens", 0) or 0
        ),
        cache_read_input_tokens=int(
            payload.get("cache_read_input_tokens", 0) or 0
        ),
        api_key_id=(
            str(payload["api_key_id"])
            if payload.get("api_key_id") else None
        ),
        workspace_id=(
            str(payload["workspace_id"])
            if payload.get("workspace_id") else None
        ),
        service_tier=(
            str(payload["service_tier"])
            if payload.get("service_tier") else None
        ),
        raw=payload,
    )


def _fetch_one_page(
    *,
    url: str,
    admin_key: str,
    timeout_s: float,
    user_agent: str,
) -> dict[str, Any]:
    """Single GET. Raises ``urllib.error.URLError`` on transport
    failure; non-2xx response is captured as ``HTTPError`` (caller
    catches both)."""
    req = urllib.request.Request(
        url=url,
        headers={
            "x-api-key": admin_key,
            "anthropic-version": ADMIN_API_VERSION,
            "User-Agent": user_agent,
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8")
    decoded: dict[str, Any] = json.loads(body)
    return decoded


def _build_url(
    *,
    base_url: str,
    starting_at: str,
    ending_at: str,
    page: str | None,
    group_by: list[str] | None,
    limit: int,
) -> str:
    params: list[tuple[str, str]] = [
        ("starting_at", starting_at),
        ("ending_at", ending_at),
        ("limit", str(limit)),
    ]
    if page:
        params.append(("page", page))
    for g in (group_by or []):
        params.append(("group_by[]", g))
    qs = urllib.parse.urlencode(params)
    return f"{base_url}/v1/organizations/usage_report/messages?{qs}"


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def fetch(
    *,
    admin_key: str,
    since: str = "30d",
    until: datetime | None = None,
    base_url: str = DEFAULT_BASE_URL,
    group_by: list[str] | None = None,
    page_limit: int = 1000,
    max_pages: int = 100,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    user_agent: str = DEFAULT_USER_AGENT,
) -> FetchResult:
    """Pull every usage record between ``since`` and ``until``.

    Walks the ``next_page`` cursor up to ``max_pages`` times. On any
    transport / parse error mid-walk, returns the partial result
    with ``error`` populated rather than raising — operators usually
    want "I got 9 of 10 pages" over "I got nothing".
    """
    if not admin_key:
        return FetchResult(
            error="ANTHROPIC_ADMIN_KEY (or --admin-key) is required",
        )
    starting_dt = _parse_since(since)
    ending_dt = until or datetime.now(UTC).replace(tzinfo=None)
    starting_at = _iso(starting_dt)
    ending_at = _iso(ending_dt)
    result = FetchResult(
        requested_starting_at=starting_at,
        requested_ending_at=ending_at,
    )
    page: str | None = None

    for _ in range(max_pages):
        url = _build_url(
            base_url=base_url,
            starting_at=starting_at,
            ending_at=ending_at,
            page=page,
            group_by=group_by,
            limit=page_limit,
        )
        try:
            data = _fetch_one_page(
                url=url, admin_key=admin_key,
                timeout_s=timeout_s, user_agent=user_agent,
            )
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
            except Exception:  # noqa: BLE001
                err_body = ""
            result.error = (
                f"HTTP {e.code}: {err_body[:200]}"
                if err_body else f"HTTP {e.code}"
            )
            return result
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            result.error = f"transport error: {e!r}"
            return result
        except json.JSONDecodeError as e:
            result.error = f"malformed JSON: {e!r}"
            return result

        for row in data.get("data", []) or []:
            if isinstance(row, dict):
                result.records.append(_record_from_payload(row))
        result.pages_fetched += 1
        page = data.get("next_page")
        if not page:
            break

    return result


def iter_billed(
    records: list[UsageRecord],
) -> Iterator[tuple[UsageRecord, float]]:
    """Yield (record, billed_dollars) pairs using the cache-aware
    pricing table from PR #1."""
    from aegis.cost.pricing import billed_dollars
    for r in records:
        yield r, billed_dollars(
            model_name=r.model,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_read_tokens=r.cache_read_input_tokens,
            cache_creation_tokens=r.cache_creation_input_tokens,
        )


def total_billed(records: list[UsageRecord]) -> float:
    """Convenience: sum of cache-aware $ across every record."""
    return sum(b for _, b in iter_billed(records))


def per_model_breakdown(
    records: list[UsageRecord],
) -> dict[str, dict[str, float]]:
    """Aggregate by model name: total tokens (input/output/cache_*)
    and total billed dollars. Returns
    ``{model: {input_tokens, output_tokens, cache_read, cache_creation,
    billed_dollars}}``."""
    out: dict[str, dict[str, float]] = {}
    for r, b in iter_billed(records):
        m = out.setdefault(
            r.model,
            {
                "input_tokens": 0.0,
                "output_tokens": 0.0,
                "cache_read": 0.0,
                "cache_creation": 0.0,
                "billed_dollars": 0.0,
                "n_records": 0.0,
            },
        )
        m["input_tokens"] += r.input_tokens
        m["output_tokens"] += r.output_tokens
        m["cache_read"] += r.cache_read_input_tokens
        m["cache_creation"] += r.cache_creation_input_tokens
        m["billed_dollars"] += b
        m["n_records"] += 1
    return out
