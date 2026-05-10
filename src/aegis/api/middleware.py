"""Production-hardening middleware for the Aegis sidecar.

Five concerns shipped here, all opt-in via :class:`Settings` flags:

* :class:`RequestSizeLimitMiddleware` — reject payloads larger than
  the configured cap with HTTP 413, *before* FastAPI parses the body.
  Defends against OOM from a runaway client posting multi-GB bodies.

* :class:`RateLimitMiddleware` — per-key (tenant_id when supplied,
  remote IP otherwise) token bucket. Returns HTTP 429 with a
  ``Retry-After`` header on burst. In-memory; multi-replica deploys
  need an external limiter (Redis, Envoy) — out of scope for the MVP.

* :class:`RequestIdMiddleware` — stamp every response with an
  ``X-Request-ID`` header so an operator can correlate a client log
  line with the audit chain entry. Honors a client-supplied request id
  when present (useful for tracing across multiple Aegis hops).

* :class:`SecurityHeadersMiddleware` — minimal hardening: ``X-Frame-
  Options: DENY``, ``X-Content-Type-Options: nosniff``,
  ``Referrer-Policy: same-origin``. We don't add a CSP because the
  static dashboard imports its own scripts and inline styles; a tight
  CSP is a separate hardening PR.

* :func:`register_error_handlers` — single source of truth for
  exception → JSON envelope conversion. Maps every leaked exception
  to ``{"error": {"code": "...", "message": "..."}}`` with no
  traceback in the body. The traceback still lands in structlog so
  the operator can debug from logs.

The middlewares are pure ASGI types — they work with any ASGI app,
not just FastAPI. The :func:`install_hardening` convenience wires
them all + the error handlers in the right order.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("aegis.middleware")


# ──────────────────────────────────────────────────────────────────
# 1. Request size limit
# ──────────────────────────────────────────────────────────────────


class RequestTooLargeError(Exception):
    """Body exceeded the configured limit. Surfaced as HTTP 413."""


class RequestSizeLimitMiddleware:
    """Pure-ASGI middleware that enforces a hard cap on request body
    size *before* FastAPI parses it.

    Two checks:

    1. ``Content-Length`` header — if present and over the cap,
       reject immediately without reading any of the body.
    2. Streaming check — for chunked / no-Content-Length requests,
       count bytes as they arrive and reject mid-stream once the
       running total crosses the cap.

    Either way, the client gets HTTP 413 with a JSON body. The audit
    chain is not touched by oversized requests — they fail before
    reaching any firewall step.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        # Fast path: Content-Length lets us reject before reading.
        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                if int(cl.decode("ascii")) > self.max_bytes:
                    await _send_json(
                        send, status=413,
                        body={"error": {
                            "code": "request_too_large",
                            "message": (
                                f"request body exceeds limit "
                                f"({self.max_bytes} bytes)"
                            ),
                        }},
                    )
                    return
            except ValueError:
                pass  # malformed Content-Length — let the app handle it

        # Slow path: count bytes as they arrive. This handles chunked
        # transfer encoding and clients that omit Content-Length.
        seen = 0
        cap = self.max_bytes

        async def metered_receive() -> Message:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                body = msg.get("body", b"")
                seen += len(body)
                if seen > cap:
                    raise RequestTooLargeError()
            return msg

        try:
            await self.app(scope, metered_receive, send)
        except RequestTooLargeError:
            await _send_json(
                send, status=413,
                body={"error": {
                    "code": "request_too_large",
                    "message": (
                        f"request body exceeds limit ({cap} bytes)"
                    ),
                }},
            )


async def _send_json(
    send: Send, *, status: int, body: dict[str, Any],
) -> None:
    import json
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


# ──────────────────────────────────────────────────────────────────
# 2. Rate limit
# ──────────────────────────────────────────────────────────────────


@dataclass
class _Bucket:
    """Token bucket — one per (key) pair.

    ``tokens`` is a float so partial accumulation between calls works
    cleanly; ``last_refill`` is wall-clock seconds.
    """
    tokens: float
    last_refill: float = field(default_factory=time.monotonic)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limit. Per-key default is generous enough that
    legitimate Claude Code traffic never hits it; the limit exists to
    throttle a runaway client / accidental loop / DoS attempt.

    Key derivation: prefer ``X-Tenant-ID`` header (if the caller knows
    its tenant), then fall back to client IP. This means a misbehaving
    *single* tenant gets throttled without affecting other tenants on
    the same sidecar instance.

    Out of scope: distributed limiter. A multi-replica deploy needs
    Redis or similar to share the bucket across replicas. Documented
    in the docstring; the in-memory limiter is correct for single-
    replica MVP and serves as a backstop on multi-replica too.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        rate_per_minute: int,
        burst: int,
        skip_paths: tuple[str, ...] = ("/healthz", "/readyz"),
    ) -> None:
        super().__init__(app)
        self.rate_per_minute = max(1, rate_per_minute)
        self.burst = max(1, burst)
        self.skip_paths = skip_paths
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    @staticmethod
    def _client_key(request: Request) -> str:
        # tenant_id wins; falls back to the raw client IP. We DO NOT
        # use a hash here — the bucket key is used internally only,
        # never logged at debug level by this module.
        tenant = request.headers.get("x-tenant-id")
        if tenant:
            return f"tenant:{tenant}"
        client = request.client
        if client is not None and client.host:
            return f"ip:{client.host}"
        return "unknown"

    def _take_token(self, key: str) -> tuple[bool, float]:
        """Refill + try to consume one token. Returns (allowed, retry_after_s)."""
        now = time.monotonic()
        per_sec = self.rate_per_minute / 60.0
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(tokens=float(self.burst), last_refill=now)
                self._buckets[key] = b
            elapsed = max(0.0, now - b.last_refill)
            b.tokens = min(float(self.burst), b.tokens + elapsed * per_sec)
            b.last_refill = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True, 0.0
            # Time-to-next-token; round up so Retry-After is at least 1s.
            need = 1.0 - b.tokens
            retry_after = max(1.0, need / per_sec)
            return False, retry_after

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Any]],
    ) -> Any:
        if request.url.path in self.skip_paths:
            return await call_next(request)

        key = self._client_key(request)
        allowed, retry_after = self._take_token(key)
        if allowed:
            return await call_next(request)

        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(int(retry_after))},
            content={"error": {
                "code": "rate_limited",
                "message": (
                    f"rate limit exceeded — retry after "
                    f"{int(retry_after)}s"
                ),
            }},
        )


# ──────────────────────────────────────────────────────────────────
# 3. Request ID
# ──────────────────────────────────────────────────────────────────


_REQUEST_ID_HEADER: Final[str] = "X-Request-ID"
_REQUEST_ID_HEADER_BYTES: Final[bytes] = b"x-request-id"


class RequestIdMiddleware:
    """Stamp every response with ``X-Request-ID``.

    Pure-ASGI middleware (rather than ``BaseHTTPMiddleware``) so the
    header is added even when an ``Exception`` handler short-circuits
    the response chain — ``BaseHTTPMiddleware``'s ``call_next`` raises
    when the inner exception handler converts the error, and the
    post-processing line never runs. Wrapping ``send`` instead lets
    us inject the header on the way out regardless of how the
    response was produced.

    Honors a client-supplied request id; otherwise mints uuid4. Also
    exposed at ``request.state.request_id`` for handlers + the
    fallback exception logger.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Pull or mint the request id from the inbound headers.
        headers = dict(scope.get("headers") or [])
        rid_bytes = headers.get(_REQUEST_ID_HEADER_BYTES)
        rid = rid_bytes.decode("ascii") if rid_bytes else uuid.uuid4().hex
        # Stash on scope.state so handlers can read it.
        state = scope.get("state")
        if state is not None:
            state["request_id"] = rid
        # Starlette also exposes scope state via Request.state — set both.
        scope.setdefault("state", {})["request_id"] = rid

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message_headers = list(message.get("headers", []))
                # Don't duplicate if a downstream already set one.
                already_set = any(
                    h[0].lower() == _REQUEST_ID_HEADER_BYTES
                    for h in message_headers
                )
                if not already_set:
                    message_headers.append(
                        (_REQUEST_ID_HEADER_BYTES, rid.encode("ascii")),
                    )
                message["headers"] = message_headers
            await send(message)

        await self.app(scope, receive, wrapped_send)


# ──────────────────────────────────────────────────────────────────
# 4. Security headers
# ──────────────────────────────────────────────────────────────────


_SECURITY_HEADERS: Final[tuple[tuple[bytes, bytes], ...]] = (
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
)


class SecurityHeadersMiddleware:
    """Minimal security headers. Pure-ASGI for the same reason
    :class:`RequestIdMiddleware` is — has to fire on 500 responses
    produced by the global exception handler too.

    Tight CSP is a separate PR — adding one here would break the
    bundled dashboard which uses inline styles and scripts.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message_headers = list(message.get("headers", []))
                existing = {h[0].lower() for h in message_headers}
                for name, value in _SECURITY_HEADERS:
                    if name not in existing:
                        message_headers.append((name, value))
                message["headers"] = message_headers
            await send(message)

        await self.app(scope, receive, wrapped_send)


# ──────────────────────────────────────────────────────────────────
# 5. Structured error responses
# ──────────────────────────────────────────────────────────────────


def _error_envelope(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _hardened_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a header dict with the security headers pre-populated.

    Used by the exception handlers because their JSONResponse is sent
    via Starlette's ``ServerErrorMiddleware``, which sits *outside*
    user middleware — meaning :class:`SecurityHeadersMiddleware`
    never gets a chance to run on these responses. Baking the
    hardening directly into the response is the explicit fix.
    """
    out = {
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
    }
    if extra:
        out.update(extra)
    return out


def register_error_handlers(app: FastAPI) -> None:
    """Install handlers that convert exceptions into the
    ``{"error": {"code": ..., "message": ...}}`` envelope.

    Three handlers:

    * :class:`StarletteHTTPException` (FastAPI's ``HTTPException``):
      preserves the status code; uses ``http_<status>`` as the error
      code. Detail is passed through as the message.
    * :class:`RequestValidationError`: 422, code ``validation_error``.
      Includes the structured ``errors`` list so clients can localize
      the field-specific failures.
    * Catch-all ``Exception``: 500, code ``internal_error``, message
      ``"unexpected server error"``. The actual exception goes to
      structlog at ERROR level — it MUST NOT leak to the client.
      This is what closes the "stack trace exposure" gap from the
      MVP review.
    """

    @app.exception_handler(StarletteHTTPException)
    async def http_exc(req: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = f"http_{exc.status_code}"
        rid = getattr(req.state, "request_id", "no-request-id")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope(
                code=code,
                message=str(exc.detail) if exc.detail else "request failed",
            ),
            headers=_hardened_headers({
                _REQUEST_ID_HEADER: rid,
                **(getattr(exc, "headers", None) or {}),
            }),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exc(
        req: Request, exc: RequestValidationError,
    ) -> JSONResponse:
        rid = getattr(req.state, "request_id", "no-request-id")
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request validation failed",
                    # The pydantic-rendered list is safe to expose: it
                    # describes WHICH field is invalid, not stack
                    # internals.
                    "errors": exc.errors(),
                },
            },
            headers=_hardened_headers({_REQUEST_ID_HEADER: rid}),
        )

    @app.exception_handler(Exception)
    async def fallback_exc(req: Request, exc: Exception) -> JSONResponse:
        rid = getattr(req.state, "request_id", "no-request-id")
        # Log the FULL exception with traceback for the operator —
        # this is the only place that sees the raw error.
        logger.exception(
            "unhandled exception (request_id=%s path=%s): %s",
            rid, req.url.path, exc,
        )
        return JSONResponse(
            status_code=500,
            content=_error_envelope(
                code="internal_error",
                message="unexpected server error",
            ),
            headers=_hardened_headers({_REQUEST_ID_HEADER: rid}),
        )


# ──────────────────────────────────────────────────────────────────
# Convenience installer
# ──────────────────────────────────────────────────────────────────


def install_hardening(
    app: FastAPI,
    *,
    max_request_bytes: int,
    rate_per_minute: int,
    rate_burst: int,
) -> None:
    """Wire all five concerns onto ``app`` in the right order.

    Order matters:

    * RequestSizeLimit goes outermost so an oversized payload never
      gets rate-limit-counted (counting it would let an attacker burn
      another client's bucket by spamming oversized bodies).
    * RateLimit second — only counts requests that survived the size
      cap.
    * RequestId third — every response (including 413/429) gets an
      X-Request-ID so the operator can correlate logs.
    * SecurityHeaders innermost (executes last on the way out).

    Error handlers don't have ordering — FastAPI dispatches by type.
    """
    # `add_middleware` adds in *outermost-first* order. Reverse the
    # logical order so the outermost concern is added last.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIdMiddleware)
    if rate_per_minute > 0:
        app.add_middleware(
            RateLimitMiddleware,
            rate_per_minute=rate_per_minute,
            burst=rate_burst,
        )
    if max_request_bytes > 0:
        app.add_middleware(
            RequestSizeLimitMiddleware,
            max_bytes=max_request_bytes,
        )

    register_error_handlers(app)
