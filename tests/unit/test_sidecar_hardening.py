"""Tests for the sidecar production-hardening middleware + lifecycle.

Five surfaces under test:

* Request size limit (413)
* Rate limit (429)
* Request id (X-Request-ID)
* Security headers
* Structured error envelope (catch-all 500 + validation 422 + http exc)
* /readyz + lifespan readiness flag

Each test boots a small FastAPI app via the canonical
:func:`aegis.api.middleware.install_hardening` so the production wiring
order is what's exercised, not a parallel test-only wiring.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from aegis.api.lifecycle import (
    LifecycleState,
    make_lifespan,
    make_readyz_router,
)
from aegis.api.middleware import (
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    install_hardening,
)

# ── small app builder ──────────────────────────────────────────────


def _app(**kwargs: Any) -> FastAPI:
    """Build a minimal FastAPI app with hardening installed.

    Routes:
        GET  /echo         → 200 plain text
        POST /echo         → 200 echo body length
        GET  /boom         → raises ValueError (catch-all 500 path)
        GET  /forbidden    → raises HTTPException 403
        POST /typed        → uses pydantic body (validation_error path)
    """
    app = FastAPI()
    install_hardening(
        app,
        max_request_bytes=kwargs.get("max_request_bytes", 1024),
        rate_per_minute=kwargs.get("rate_per_minute", 0),
        rate_burst=kwargs.get("rate_burst", 0),
    )

    @app.get("/echo")
    def get_echo() -> dict[str, str]:
        return {"hello": "world"}

    @app.post("/echo")
    async def post_echo(req: Request) -> dict[str, int]:
        body = await req.body()
        return {"bytes": len(body)}

    @app.get("/boom")
    def boom() -> None:
        raise ValueError("intentional crash with 'sensitive' detail")

    @app.get("/forbidden")
    def forbidden() -> None:
        raise HTTPException(status_code=403, detail="nope")

    class Payload(BaseModel):
        n: int

    @app.post("/typed")
    def post_typed(p: Payload) -> dict[str, int]:
        return {"n": p.n}

    return app


# ── 1. RequestSizeLimit ────────────────────────────────────────────


def test_size_limit_rejects_large_content_length() -> None:
    """413 with structured error envelope when Content-Length is over."""
    app = _app(max_request_bytes=100)
    client = TestClient(app)
    body = b"x" * 200
    res = client.post("/echo", content=body)
    assert res.status_code == 413
    body_json = res.json()
    assert body_json["error"]["code"] == "request_too_large"


def test_size_limit_passes_within_cap() -> None:
    app = _app(max_request_bytes=10_000)
    client = TestClient(app)
    res = client.post("/echo", content=b"x" * 500)
    assert res.status_code == 200
    assert res.json()["bytes"] == 500


def test_size_limit_disabled_when_zero() -> None:
    """max_request_bytes=0 means no cap → big payload goes through."""
    app = _app(max_request_bytes=0)
    client = TestClient(app)
    res = client.post("/echo", content=b"x" * 100_000)
    assert res.status_code == 200


# ── 2. RateLimit ───────────────────────────────────────────────────


def test_rate_limit_allows_under_burst() -> None:
    app = _app(rate_per_minute=600, rate_burst=10)
    client = TestClient(app)
    for _ in range(10):
        res = client.get("/echo", headers={"X-Tenant-ID": "t1"})
        assert res.status_code == 200


def test_rate_limit_blocks_over_burst_with_retry_after() -> None:
    app = _app(rate_per_minute=60, rate_burst=2)
    client = TestClient(app)
    headers = {"X-Tenant-ID": "t1"}
    # Exhaust the burst (2) + drain the bucket cleanly.
    for _ in range(2):
        assert client.get("/echo", headers=headers).status_code == 200
    # Next request should fall over the limit.
    res = client.get("/echo", headers=headers)
    assert res.status_code == 429
    assert res.json()["error"]["code"] == "rate_limited"
    assert int(res.headers["retry-after"]) >= 1


def test_rate_limit_per_tenant_isolation() -> None:
    """A misbehaving tenant doesn't take down its peers."""
    app = _app(rate_per_minute=60, rate_burst=1)
    client = TestClient(app)
    # tenant-a burns its single token
    assert client.get("/echo", headers={"X-Tenant-ID": "a"}).status_code == 200
    assert client.get("/echo", headers={"X-Tenant-ID": "a"}).status_code == 429
    # tenant-b still has its own token
    assert client.get("/echo", headers={"X-Tenant-ID": "b"}).status_code == 200


def test_rate_limit_skips_healthz_and_readyz() -> None:
    """The default skip set lets a load balancer probe even when the
    bucket is empty — otherwise we'd get marked down for our own
    health checks."""
    app = FastAPI()
    install_hardening(
        app, max_request_bytes=0, rate_per_minute=60, rate_burst=1,
    )
    state = LifecycleState()
    app.include_router(make_readyz_router(state))

    @app.get("/healthz")
    def hz() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    # Burn the burst on a non-skipped path.
    assert client.get("/echo").status_code in (200, 404)  # /echo doesn't exist here
    # Still allowed even with empty bucket.
    for _ in range(5):
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


# ── 3. RequestId ───────────────────────────────────────────────────


def test_request_id_minted_when_absent() -> None:
    app = _app()
    client = TestClient(app)
    res = client.get("/echo")
    rid = res.headers.get("x-request-id")
    assert rid
    # Looks roughly like a uuid hex.
    assert len(rid) == 32 and all(c in "0123456789abcdef" for c in rid)


def test_request_id_honored_when_supplied() -> None:
    app = _app()
    client = TestClient(app)
    res = client.get("/echo", headers={"X-Request-ID": "trace-from-upstream"})
    assert res.headers["x-request-id"] == "trace-from-upstream"


# ── 4. Security headers ────────────────────────────────────────────


def test_security_headers_set_on_every_response() -> None:
    app = _app()
    client = TestClient(app)
    res = client.get("/echo")
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "same-origin"


def test_security_headers_set_on_error_responses_too() -> None:
    """A 500 still gets hardening headers — important so a sniffed
    error page can't be reframed."""
    app = _app()
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/boom")
    assert res.status_code == 500
    assert res.headers.get("x-frame-options") == "DENY"


# ── 5. Structured error envelope ───────────────────────────────────


def test_unhandled_exception_returns_envelope_not_traceback() -> None:
    """The killer test: a leaked exception MUST NOT include the
    traceback or the original exception message in the response body.
    The MVP review specifically called out 'no stack traces leaked'."""
    app = _app()
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/boom")
    assert res.status_code == 500
    body = res.json()
    assert body == {"error": {
        "code": "internal_error",
        "message": "unexpected server error",
    }}
    # The original error message must NOT appear.
    assert "intentional crash" not in res.text
    assert "sensitive" not in res.text


def test_http_exception_returns_envelope_with_status_code() -> None:
    app = _app()
    client = TestClient(app)
    res = client.get("/forbidden")
    assert res.status_code == 403
    body = res.json()
    assert body["error"]["code"] == "http_403"
    assert body["error"]["message"] == "nope"


def test_validation_error_returns_envelope_with_field_details() -> None:
    app = _app()
    client = TestClient(app)
    res = client.post("/typed", json={"n": "not-an-int"})
    assert res.status_code == 422
    body = res.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"] == "request validation failed"
    # Field-specific error list is present (helps clients localize).
    assert isinstance(body["error"]["errors"], list)
    assert len(body["error"]["errors"]) >= 1


def test_unhandled_exception_response_carries_request_id() -> None:
    """An operator chasing a 500 needs to grep `request_id=...` in
    structlog and find the corresponding request — the X-Request-ID
    header must be present on the error response too."""
    app = _app()
    client = TestClient(app, raise_server_exceptions=False)
    rid = "trace-for-the-500"
    res = client.get("/boom", headers={"X-Request-ID": rid})
    assert res.headers.get("x-request-id") == rid


# ── 6. /readyz + lifecycle ────────────────────────────────────────


def test_readyz_200_when_state_ready_and_probes_pass() -> None:
    state = LifecycleState()
    app = FastAPI()
    install_hardening(app, max_request_bytes=0, rate_per_minute=0, rate_burst=0)
    app.include_router(make_readyz_router(
        state, probes={"x": lambda: True, "y": lambda: True},
    ))
    client = TestClient(app)
    res = client.get("/readyz")
    assert res.status_code == 200
    body = res.json()
    assert body["ready"] is True
    assert body["checks"] == {"x": True, "y": True}
    assert body["shutting_down"] is False
    assert body["uptime_s"] >= 0


def test_readyz_503_when_a_probe_fails() -> None:
    state = LifecycleState()
    app = FastAPI()
    install_hardening(app, max_request_bytes=0, rate_per_minute=0, rate_burst=0)
    app.include_router(make_readyz_router(
        state, probes={"healthy": lambda: True, "broken": lambda: False},
    ))
    client = TestClient(app)
    res = client.get("/readyz")
    assert res.status_code == 503
    body = res.json()
    assert body["ready"] is False
    assert body["checks"] == {"healthy": True, "broken": False}


def test_readyz_503_when_probe_raises() -> None:
    """A probe that throws is treated as 'failed' — never crashes the
    /readyz handler itself."""
    def boom() -> bool:
        raise RuntimeError("probe broken")

    state = LifecycleState()
    app = FastAPI()
    install_hardening(app, max_request_bytes=0, rate_per_minute=0, rate_burst=0)
    app.include_router(make_readyz_router(state, probes={"flaky": boom}))
    client = TestClient(app)
    res = client.get("/readyz")
    assert res.status_code == 503
    assert res.json()["checks"] == {"flaky": False}


def test_readyz_503_when_state_marked_shutting_down() -> None:
    state = LifecycleState()
    state.ready = False
    app = FastAPI()
    install_hardening(app, max_request_bytes=0, rate_per_minute=0, rate_burst=0)
    app.include_router(make_readyz_router(state))
    client = TestClient(app)
    res = client.get("/readyz")
    assert res.status_code == 503
    body = res.json()
    assert body["ready"] is False
    assert body["shutting_down"] is True


def test_lifespan_runs_flush_callbacks_on_shutdown() -> None:
    """The flush_callbacks tuple is invoked once each on lifespan
    exit. Failures in one don't prevent others from running."""
    state = LifecycleState()
    calls: list[str] = []

    def good() -> None:
        calls.append("good")

    def bad() -> None:
        calls.append("bad-before-raise")
        raise RuntimeError("flush flaked")

    def also_good() -> None:
        calls.append("also-good")

    lifespan = make_lifespan(
        state, drain_seconds=0.0,
        flush_callbacks=(good, bad, also_good),
    )
    app = FastAPI(lifespan=lifespan)

    # Entering + leaving the TestClient context manager fires the
    # full lifespan. After exit, state.ready is False and all three
    # callbacks have run — the bad one raised but didn't break the
    # chain.
    with TestClient(app):
        assert state.ready is True
    assert state.ready is False
    assert calls == ["good", "bad-before-raise", "also-good"]


def test_full_app_serves_readyz_via_create_app() -> None:
    """Smoke-test the production wiring: ``aegis.main.create_app()``
    produces an app that serves both /healthz and /readyz."""
    from aegis.main import create_app
    app = create_app()
    client = TestClient(app)
    health = client.get("/healthz")
    ready = client.get("/readyz")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert ready.status_code == 200
    assert ready.json()["ready"] is True


# ── ASGI bypass for non-http requests ───────────────────────────


def test_size_middleware_passthrough_for_non_http_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: lifespan/websocket scopes shouldn't go through the
    body-size check (they have no body). The middleware passes such
    scopes straight through."""
    seen: list[Any] = []

    async def echo_app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    mw = RequestSizeLimitMiddleware(echo_app, max_bytes=100)

    async def fake_recv() -> dict[str, Any]:
        return {"type": "lifespan.startup"}

    async def fake_send(_msg: Any) -> None:
        pass

    import asyncio
    asyncio.run(mw({"type": "lifespan"}, fake_recv, fake_send))
    assert seen == ["lifespan"]


# ── rate-limit refill timing (cheap, doesn't need real sleep) ───


def test_rate_limit_refills_over_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch time.monotonic to fast-forward a minute and verify the
    bucket refills."""
    # 60 per minute = 1 per second; burst 1 → token refills in 1 sec.
    mw = RateLimitMiddleware(
        app=lambda *a, **kw: None,  # type: ignore[arg-type]
        rate_per_minute=60,
        burst=1,
    )
    now_ref = [time.monotonic()]

    def fake_now() -> float:
        return now_ref[0]

    monkeypatch.setattr("aegis.api.middleware.time.monotonic", fake_now)

    allowed, _ = mw._take_token("k")
    assert allowed is True
    # Immediate next attempt → empty bucket.
    allowed, retry = mw._take_token("k")
    assert allowed is False
    assert retry >= 1
    # Fast-forward 2 seconds → 2 tokens accumulated, capped at burst=1.
    now_ref[0] += 2.0
    allowed, _ = mw._take_token("k")
    assert allowed is True
