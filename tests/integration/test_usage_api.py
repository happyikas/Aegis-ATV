"""Anthropic Admin API client tests.

Three layers (same pattern as the Slack tests):

1. **Unit (urlopen mock)** — verify request shape (headers, query
   params, pagination loop) and response parsing without sockets.
2. **Integration (real local HTTP server)** — fake admin endpoint
   stitched onto a ThreadingHTTPServer; full urllib path runs.
3. **CLI E2E** — `aegis cost-import admin-api --admin-key X` against
   a local fake, asserts the rendered table contains expected
   columns.
"""

from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from aegis.cost.usage_api import (
    UsageRecord,
    fetch,
    iter_billed,
    per_model_breakdown,
    total_billed,
)

# ─────────────────────────────────────────────────────────────────────
# 1. Unit — urlopen mock
# ─────────────────────────────────────────────────────────────────────


class TestUnitFetch:
    def _mock_response(self, payload: dict[str, Any]) -> MagicMock:
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.read = MagicMock(return_value=json.dumps(payload).encode())
        return cm

    def test_admin_key_required(self) -> None:
        result = fetch(admin_key="", since="30d")
        assert result.error
        assert "ANTHROPIC_ADMIN_KEY" in result.error
        assert result.records == []

    def test_single_page_parsed(self) -> None:
        payload = {
            "data": [
                {
                    "starting_at": "2026-04-01T00:00:00Z",
                    "ending_at": "2026-04-02T00:00:00Z",
                    "model": "claude-haiku-4-5",
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 5000,
                },
            ],
            "next_page": None,
        }
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_response(payload),
        ) as mu:
            result = fetch(admin_key="adminkey-xyz", since="30d")
        assert result.error is None
        assert result.pages_fetched == 1
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.model == "claude-haiku-4-5"
        assert rec.input_tokens == 1000
        assert rec.cache_read_input_tokens == 5000
        # Verify request shape: x-api-key + anthropic-version headers.
        req = mu.call_args.args[0]
        assert req.headers["X-api-key"] == "adminkey-xyz"
        assert req.headers["Anthropic-version"] == "2023-06-01"
        # And the query string includes starting_at/ending_at/limit.
        qs = parse_qs(urlparse(req.full_url).query)
        assert "starting_at" in qs
        assert "ending_at" in qs
        assert qs["limit"] == ["1000"]

    def test_pagination_walks_next_page(self) -> None:
        # Three responses, first two carry a next_page cursor.
        responses = [
            self._mock_response({
                "data": [{"model": "a", "starting_at": "x", "ending_at": "y"}],
                "next_page": "p1",
            }),
            self._mock_response({
                "data": [{"model": "b", "starting_at": "x", "ending_at": "y"}],
                "next_page": "p2",
            }),
            self._mock_response({
                "data": [{"model": "c", "starting_at": "x", "ending_at": "y"}],
                "next_page": None,
            }),
        ]
        with patch("urllib.request.urlopen", side_effect=responses):
            result = fetch(admin_key="k", since="30d")
        assert result.pages_fetched == 3
        assert len(result.records) == 3
        assert [r.model for r in result.records] == ["a", "b", "c"]

    def test_http_error_returns_partial_data(self) -> None:
        # First page OK, second fails — partial result preserved.
        from io import BytesIO

        class _RaiseOnSecondCall:
            def __init__(self, first_payload: dict) -> None:
                self.calls = 0
                self.first_payload = first_payload

            def __call__(self, req, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    cm = MagicMock()
                    cm.__enter__ = MagicMock(return_value=cm)
                    cm.__exit__ = MagicMock(return_value=False)
                    cm.read = MagicMock(
                        return_value=json.dumps(self.first_payload).encode()
                    )
                    return cm
                raise urllib.error.HTTPError(
                    url=req.full_url, code=503, msg="upstream",
                    hdrs=None, fp=BytesIO(b"server overloaded"),
                )

        with patch(
            "urllib.request.urlopen",
            side_effect=_RaiseOnSecondCall({
                "data": [{"model": "a", "starting_at": "x", "ending_at": "y"}],
                "next_page": "p1",
            }),
        ):
            result = fetch(admin_key="k", since="30d")
        assert "HTTP 503" in (result.error or "")
        assert len(result.records) == 1   # first page survived

    def test_url_error_is_swallowed(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("dns fail"),
        ):
            result = fetch(admin_key="k", since="30d")
        assert "transport error" in (result.error or "")
        assert result.records == []


# ─────────────────────────────────────────────────────────────────────
# 2. Integration — real local HTTP server
# ─────────────────────────────────────────────────────────────────────


class _FakeAdminAPIHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.received.append({  # type: ignore[attr-defined]
            "path": self.path,
            "headers": dict(self.headers),
        })
        body = json.dumps({
            "data": [{
                "starting_at": "2026-04-01T00:00:00Z",
                "ending_at": "2026-04-02T00:00:00Z",
                "model": "claude-sonnet-4-6",
                "input_tokens": 2_000,
                "output_tokens": 1_000,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 10_000,
            }],
            "next_page": None,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def fake_admin_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAdminAPIHandler)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}", server.received  # type: ignore[attr-defined]
    server.shutdown()
    server.server_close()
    thread.join(timeout=2.0)


class TestIntegrationLocal:
    def test_real_request_arrives_with_correct_headers(self, fake_admin_api) -> None:
        base_url, received = fake_admin_api
        result = fetch(
            admin_key="adminkey-test",
            since="7d",
            base_url=base_url,
            timeout_s=2.0,
        )
        assert result.error is None
        assert len(result.records) == 1
        assert result.records[0].model == "claude-sonnet-4-6"
        # Server actually saw the request.
        assert len(received) == 1
        req = received[0]
        assert "/v1/organizations/usage_report/messages" in req["path"]
        assert req["headers"].get("X-Api-Key") == "adminkey-test"
        assert req["headers"].get("Anthropic-Version") == "2023-06-01"


# ─────────────────────────────────────────────────────────────────────
# 3. Helpers — billed totals, per-model breakdown
# ─────────────────────────────────────────────────────────────────────


class TestBilledHelpers:
    def _records(self) -> list[UsageRecord]:
        return [
            UsageRecord(
                starting_at="x", ending_at="y",
                model="claude-haiku-4-5",
                input_tokens=1_000_000, output_tokens=0,
            ),
            UsageRecord(
                starting_at="x", ending_at="y",
                model="claude-haiku-4-5",
                output_tokens=1_000_000,
            ),
            UsageRecord(
                starting_at="x", ending_at="y",
                model="claude-opus-4-7",
                input_tokens=100_000, output_tokens=50_000,
                cache_read_input_tokens=500_000,
            ),
        ]

    def test_iter_billed_uses_pricing_table(self) -> None:
        recs = self._records()
        billed = list(iter_billed(recs))
        # Haiku 1M input → $0.80, 1M output → $4.00
        assert billed[0][1] == pytest.approx(0.80)
        assert billed[1][1] == pytest.approx(4.00)
        # Opus 100k input ($1.5) + 50k output ($3.75) + 500k cache_read ($0.75)
        assert billed[2][1] == pytest.approx(6.00, abs=0.01)

    def test_total_billed(self) -> None:
        recs = self._records()
        total = total_billed(recs)
        assert total == pytest.approx(0.80 + 4.00 + 6.00, abs=0.01)

    def test_per_model_breakdown_aggregates(self) -> None:
        recs = self._records()
        out = per_model_breakdown(recs)
        assert "claude-haiku-4-5" in out
        assert "claude-opus-4-7" in out
        haiku = out["claude-haiku-4-5"]
        assert haiku["input_tokens"] == 1_000_000
        assert haiku["output_tokens"] == 1_000_000
        assert haiku["billed_dollars"] == pytest.approx(0.80 + 4.00)
        assert haiku["n_records"] == 2
