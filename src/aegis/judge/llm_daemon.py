"""Tiny Unix-socket daemon that keeps the local sLLM in memory.

Why this exists
---------------
Each PreToolUse hook is a fresh ``python3`` subprocess (Claude Code's
plugin model). The LRU cache in :func:`local_phi._load_real_phi`
doesn't survive across processes, so every hook call that escalates
to step340 LLM pays the cold load:

* Llama-3.2-1B Q4 cold:    ~2.1 s  (under 5 s timeout, but slow)
* Phi-3.5-mini Q4 cold:    ~6.5 s  (EXCEEDS the timeout)

Everything else in the firewall (M13, RAG, drift, audit chain) runs
fast in-process. The only path that benefits from a long-lived
process is the LLM call.

Architecture
------------
This module implements a **tiny one-purpose daemon**: it loads the
GGUF judge once at startup and serves ``evaluate`` requests over a
Unix socket. The hook process keeps doing everything else (ATV
build, M13, RAG, audit) in-process — only the LLM inference round-
trips through the daemon.

* **Wire protocol:** newline-delimited JSON. One request per line,
  one response per line, then the connection closes.
* **Concurrency:** llama-cpp's ``Llama`` is not thread-safe; the
  daemon serialises requests through a lock. Solo Free is single-
  user so this is fine — Claude Code doesn't fire concurrent hooks.
* **Lifecycle:** ``aegis sidecar start`` daemonises this module.
  ``stop`` sends SIGTERM. ``status`` checks the PID file + socket.
* **Fallback:** if the socket can't be reached, ``LocalPhiJudge``
  silently falls back to in-process loading (the pre-PR-#30
  behaviour). Daemon presence is purely an optimisation.

The daemon does NOT run the firewall pipeline. It does NOT touch the
audit chain. It's an inference cache, not a security boundary —
adding privileges to a long-lived process would change the threat
model.

Files in ``~/.aegis/``
----------------------
* ``llm_sidecar.sock``  — Unix socket the daemon listens on.
* ``llm_sidecar.pid``   — PID + model_path JSON, written on start.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SOCK_PATH = Path.home() / ".aegis" / "llm_sidecar.sock"
DEFAULT_PID_PATH = Path.home() / ".aegis" / "llm_sidecar.pid"

# Limit how big a single request line can be. Plenty for our prompts
# (~600 tok ≈ 3 kB); 256 kB is the hard ceiling so a malformed line
# doesn't blow up RAM.
_MAX_LINE = 256 * 1024


def _sock_path() -> Path:
    """Honour ``$AEGIS_SIDECAR_SOCK`` for testing; default otherwise."""
    raw = os.environ.get("AEGIS_SIDECAR_SOCK", "").strip()
    return Path(raw) if raw else DEFAULT_SOCK_PATH


def _pid_path() -> Path:
    raw = os.environ.get("AEGIS_SIDECAR_PID", "").strip()
    return Path(raw) if raw else DEFAULT_PID_PATH


# ─────────────────────────────────────────────────────────────────────
# Wire helpers — read/write one JSON object per line
# ─────────────────────────────────────────────────────────────────────


def _write_msg(sock: socket.socket, obj: dict[str, Any]) -> None:
    data = (json.dumps(obj) + "\n").encode("utf-8")
    if len(data) > _MAX_LINE:
        raise ValueError(f"message too large ({len(data)} > {_MAX_LINE})")
    sock.sendall(data)


def _read_msg(sock: socket.socket, timeout_s: float = 30.0) -> dict[str, Any]:
    sock.settimeout(timeout_s)
    buf = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in chunk:
            break
        if len(buf) > _MAX_LINE:
            raise ValueError("response too large")
    if not buf:
        raise ConnectionError("empty response from daemon")
    line = bytes(buf).split(b"\n", 1)[0]
    return dict(json.loads(line.decode("utf-8")))


# ─────────────────────────────────────────────────────────────────────
# Server side
# ─────────────────────────────────────────────────────────────────────


@dataclass
class DaemonState:
    """Per-process state carried by the daemon."""

    llm: Any                         # llama_cpp.Llama instance
    model_path: str
    model_hash: str
    started_at: float
    request_count: int = 0
    inference_lock: threading.Lock = field(default_factory=threading.Lock)


def _load_state(model_path: str) -> DaemonState:
    """Load the GGUF + compute model_hash. Called once at daemon start."""
    from aegis.judge.local_phi import _hash_model_file, _load_real_phi

    llm = _load_real_phi(model_path)
    if llm is None:
        raise RuntimeError(
            f"failed to load GGUF at {model_path} — llama-cpp-python may "
            f"be missing (uv sync --extra local-llm) or the file may be "
            f"corrupt"
        )
    return DaemonState(
        llm=llm,
        model_path=model_path,
        model_hash=_hash_model_file(model_path),
        started_at=time.time(),
    )


def _handle_request(state: DaemonState, req: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a single client message."""
    op = req.get("op", "")
    if op == "ping":
        return {
            "ok": True,
            "model_hash": state.model_hash,
            "model_path": state.model_path,
            "uptime_s": round(time.time() - state.started_at, 3),
            "requests_served": state.request_count,
        }
    if op == "evaluate":
        from aegis.judge.local_phi import _real_evaluate

        summary = str(req.get("summary", ""))
        attribution = dict(req.get("attribution") or {})
        rag_block = str(req.get("rag_block") or "")
        with state.inference_lock:
            t0 = time.perf_counter_ns()
            decision, confidence, reason = _real_evaluate(
                state.llm, summary,
                {k: float(v) for k, v in attribution.items()},
                rag_block=rag_block,
            )
            elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000
        state.request_count += 1
        return {
            "ok": True,
            "decision": decision,
            "confidence": float(confidence),
            "reason": reason,
            "model_hash": state.model_hash,
            "latency_ms": round(elapsed_ms, 3),
        }
    return {"ok": False, "error": f"unknown op {op!r}"}


def _serve_one(state: DaemonState, conn: socket.socket) -> None:
    """Serve one client connection (read one msg, write one response)."""
    try:
        try:
            req = _read_msg(conn, timeout_s=30.0)
        except (TimeoutError, ValueError, json.JSONDecodeError, OSError) as e:
            with contextlib.suppress(OSError):
                _write_msg(conn, {"ok": False, "error": f"bad request: {e}"})
            return
        try:
            resp = _handle_request(state, req)
        except Exception as e:  # noqa: BLE001 - never crash the daemon
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        with contextlib.suppress(OSError):
            _write_msg(conn, resp)
    finally:
        with contextlib.suppress(OSError):
            conn.close()


def serve_forever(
    model_path: str,
    *,
    sock_path: Path | None = None,
    pid_path: Path | None = None,
) -> None:
    """Bind the Unix socket, load the model, accept connections forever.

    Writes the daemon PID + model path to ``pid_path`` so ``aegis
    sidecar status`` can find it. SIGTERM cleanly removes the socket
    and PID file before exiting.
    """
    sp = sock_path or _sock_path()
    pp = pid_path or _pid_path()

    sp.parent.mkdir(parents=True, exist_ok=True)
    pp.parent.mkdir(parents=True, exist_ok=True)
    if sp.exists():
        sp.unlink()

    state = _load_state(model_path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sp))
    server.listen(8)

    pp.write_text(json.dumps({
        "pid": os.getpid(),
        "model_path": model_path,
        "model_hash": state.model_hash,
        "sock_path": str(sp),
        "started_at_ns": time.time_ns(),
    }))

    stop = threading.Event()

    def _on_signal(_signo: int, _frame: Any) -> None:
        stop.set()
        with contextlib.suppress(OSError):
            server.close()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except OSError:
                # Socket closed by signal handler — shutdown.
                break
            _serve_one(state, conn)
    finally:
        with contextlib.suppress(OSError):
            sp.unlink()
        with contextlib.suppress(OSError):
            pp.unlink()


# ─────────────────────────────────────────────────────────────────────
# Client side — used by LocalPhiJudge to bypass cold-load
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DaemonResponse:
    decision: str
    confidence: float
    reason: str
    model_hash: str
    latency_ms: float


class DaemonClient:
    """Minimal client. Returns ``None`` on any failure — caller falls
    back to in-process LLM load."""

    def __init__(self, sock_path: Path | None = None) -> None:
        self.sock_path = sock_path or _sock_path()

    def is_running(self) -> bool:
        return self.sock_path.exists() and self.sock_path.is_socket()

    def evaluate(
        self,
        summary: str,
        attribution: dict[str, float],
        rag_block: str = "",
        *,
        timeout_s: float = 30.0,
    ) -> DaemonResponse | None:
        """Send one ``evaluate`` request. Returns None on any error.

        Errors covered:
        * socket file missing / not a socket → daemon not running
        * connect refused → daemon stale (PID file but no listener)
        * timeout → request line too long, daemon hung, or model OOM
        * bad response → daemon corruption

        Each is treated identically — return None and let the caller
        fall back to the pre-PR-#30 in-process load path. Error text
        does NOT propagate to the user-facing audit log; the audit
        record's ``reason`` will say "local-phi (parsed): ..." as
        before, just from the in-process path.
        """
        if not self.is_running():
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
            sock.connect(str(self.sock_path))
            try:
                _write_msg(sock, {
                    "op": "evaluate",
                    "summary": summary,
                    "attribution": dict(attribution),
                    "rag_block": rag_block,
                })
                resp = _read_msg(sock, timeout_s=timeout_s)
            finally:
                with contextlib.suppress(OSError):
                    sock.close()
        except (OSError, ValueError, json.JSONDecodeError):
            return None

        if not resp.get("ok"):
            return None
        try:
            return DaemonResponse(
                decision=str(resp["decision"]),
                confidence=float(resp["confidence"]),
                reason=str(resp["reason"]),
                model_hash=str(resp["model_hash"]),
                latency_ms=float(resp["latency_ms"]),
            )
        except (KeyError, ValueError, TypeError):
            return None

    def ping(self, *, timeout_s: float = 2.0) -> dict[str, Any] | None:
        """Health check + introspection. Returns the daemon's status dict."""
        if not self.is_running():
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
            sock.connect(str(self.sock_path))
            try:
                _write_msg(sock, {"op": "ping"})
                resp = _read_msg(sock, timeout_s=timeout_s)
            finally:
                with contextlib.suppress(OSError):
                    sock.close()
            return resp if resp.get("ok") else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None


# ─────────────────────────────────────────────────────────────────────
# CLI helpers used by ``aegis sidecar``
# ─────────────────────────────────────────────────────────────────────


def read_pid_file(pid_path: Path | None = None) -> dict[str, Any] | None:
    """Read the PID file. Returns None if absent/malformed."""
    pp = pid_path or _pid_path()
    if not pp.exists():
        return None
    try:
        return dict(json.loads(pp.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def is_pid_alive(pid: int) -> bool:
    """``kill -0`` style liveness check."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


__all__ = [
    "DEFAULT_PID_PATH",
    "DEFAULT_SOCK_PATH",
    "DaemonClient",
    "DaemonResponse",
    "DaemonState",
    "is_pid_alive",
    "read_pid_file",
    "serve_forever",
]
