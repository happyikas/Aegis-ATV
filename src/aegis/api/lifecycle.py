"""Lifecycle: graceful shutdown + the ``/readyz`` readiness endpoint.

Two concerns shipped here:

* **Graceful shutdown** — :func:`make_lifespan` returns an
  ``@asynccontextmanager`` suitable for FastAPI's ``lifespan=`` param.
  On SIGTERM, it sets a "ready=False" flag (so /readyz starts
  returning 503 and load balancers stop sending new traffic), waits
  briefly for in-flight requests to drain, and flushes anything
  buffered (group-commit journals, intent log).

* **Readiness vs. liveness** — :func:`make_readyz_endpoint` returns
  a router with ``GET /readyz`` that checks every wired-up store can
  be touched (cheap reads). Distinct from ``/healthz`` which only
  proves the process is alive. Kubernetes / Compose health probes
  should hit ``/readyz`` for traffic gating; ``/healthz`` for
  restart decisions.

Why split lifespan into a builder + a separate /readyz router:

* Tests can install just one or the other.
* The /readyz endpoint takes its dep handles via closure, which is
  a clean fit for the existing ``create_app`` pattern (no new module
  globals).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

logger = logging.getLogger("aegis.lifecycle")


@dataclass
class LifecycleState:
    """Mutable state shared between ``/readyz`` and the lifespan
    handler. ``ready=True`` from boot until SIGTERM.

    Threadsafe-ish: writes happen on the asyncio loop only (one
    writer at a time); readers can race but with no consequence
    (eventually-consistent ready flag).
    """
    ready: bool = True
    started_at_ns: int = field(default_factory=time.time_ns)


def make_lifespan(
    state: LifecycleState,
    *,
    drain_seconds: float = 5.0,
    flush_callbacks: tuple[Callable[[], Any], ...] = (),
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build a FastAPI lifespan that handles graceful shutdown.

    On startup: nothing — ``state.ready`` is True from
    construction. On shutdown:

      1. Flip ``state.ready = False`` so ``/readyz`` returns 503 and
         the load balancer stops routing new traffic.
      2. Wait ``drain_seconds`` for in-flight handlers to finish.
         Tunable so a deployment with longer-tailed handlers can
         extend the grace window. Doesn't *enforce* completion —
         when this elapses we just stop waiting; uvicorn handles
         the actual request cancellation.
      3. Run any registered flush callbacks (group-commit journals,
         intent log, etc.). Each callback's exceptions are swallowed
         so a flaky flush doesn't prevent shutdown.

    Usage::

        state = LifecycleState()
        app = FastAPI(lifespan=make_lifespan(state, flush_callbacks=(...,)))
    """
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("aegis sidecar startup — ready=True")
        try:
            yield
        finally:
            state.ready = False
            logger.info(
                "aegis sidecar shutdown — ready=False, "
                "draining for %.1fs", drain_seconds,
            )
            # Best-effort sleep to let in-flight handlers complete.
            # We don't track the actual in-flight set; that's
            # uvicorn's job. We just stop accepting new traffic via
            # /readyz=False and give a grace window before the
            # process exits.
            try:
                import asyncio
                await asyncio.sleep(drain_seconds)
            except Exception:  # noqa: BLE001 — defensive
                pass
            for cb in flush_callbacks:
                try:
                    cb()
                except Exception as e:  # noqa: BLE001 — best-effort
                    logger.warning("flush callback failed: %s", e)
            logger.info("aegis sidecar shutdown complete")

    return lifespan


def make_readyz_router(
    state: LifecycleState,
    *,
    probes: dict[str, Callable[[], bool]] | None = None,
) -> APIRouter:
    """Build a router serving ``GET /readyz``.

    Returns 200 with ``{"ready": true, "uptime_s": ..., "checks":
    {...}}`` when every probe in ``probes`` returns True AND
    ``state.ready`` is True.

    Returns 503 with the same shape (but ``"ready": false``) on
    shutdown OR if any probe fails. Always JSON, always parseable —
    a load balancer can decode without special-casing.

    Probes should be **cheap** (under ~10ms each). They run
    synchronously in the request handler. If you need an expensive
    check (e.g. "DB connectivity"), wrap it in a circuit breaker so
    /readyz doesn't block on a slow back-end.
    """
    router = APIRouter()
    probes = probes or {}

    @router.get("/readyz")
    def readyz() -> JSONResponse:
        check_results: dict[str, bool] = {}
        all_ok = state.ready
        for name, probe in probes.items():
            try:
                ok = bool(probe())
            except Exception as e:  # noqa: BLE001 — defensive
                logger.warning("/readyz probe %r failed: %s", name, e)
                ok = False
            check_results[name] = ok
            if not ok:
                all_ok = False

        body = {
            "ready": all_ok,
            "uptime_s": (time.time_ns() - state.started_at_ns) // 1_000_000_000,
            "checks": check_results,
            "shutting_down": not state.ready,
        }
        return JSONResponse(
            status_code=200 if all_ok else 503,
            content=body,
        )

    return router
