"""Unit tests for the LLM-keep-alive daemon (PR #30).

Tests cover the wire protocol, client fall-back semantics, PID-file
lifecycle, and CLI argparse — but NOT the real model load (that's
exercised in tests/integration/test_real_sllm_e2e.py and the dogfood
``[16]`` check).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from aegis.judge.llm_daemon import (
    DaemonClient,
    is_pid_alive,
    read_pid_file,
)

# ─────────────────────────────────────────────────────────────────────
# Wire helpers
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, threading.Thread]:
    """Spin up a tiny mock server on a Unix socket. The server echoes
    a deterministic response — we test the client + protocol without
    needing llama-cpp.

    Uses ``/tmp/`` rather than pytest's ``tmp_path`` because macOS caps
    Unix socket paths at ~104 chars; pytest's nested ``tmp_path``
    typically blows past that.
    """
    import tempfile
    import uuid
    short_dir = Path(tempfile.gettempdir())
    sock_path = short_dir / f"aegis-test-{uuid.uuid4().hex[:8]}.sock"
    monkeypatch.setenv("AEGIS_SIDECAR_SOCK", str(sock_path))

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(4)

    def _serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            try:
                data = conn.recv(64 * 1024)
                req = json.loads(data.decode("utf-8").split("\n", 1)[0])
                if req.get("op") == "ping":
                    resp = {
                        "ok": True,
                        "model_hash": "0123" * 16,
                        "model_path": "/mock.gguf",
                        "uptime_s": 1.0,
                        "requests_served": 0,
                    }
                elif req.get("op") == "evaluate":
                    # Simple deterministic verdict based on summary keyword.
                    summary = str(req.get("summary", "")).lower()
                    if "rm -rf" in summary or "drop table" in summary:
                        decision, conf = "BLOCK", 0.9
                    elif "approval" in summary:
                        decision, conf = "REQUIRE_APPROVAL", 0.7
                    else:
                        decision, conf = "ALLOW", 0.6
                    resp = {
                        "ok": True,
                        "decision": decision,
                        "confidence": conf,
                        "reason": f"mock daemon: {decision}",
                        "model_hash": "0123" * 16,
                        "latency_ms": 5.0,
                    }
                else:
                    resp = {"ok": False, "error": f"unknown op {req.get('op')!r}"}
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            except Exception as e:  # noqa: BLE001 — never crash the test
                import contextlib
                with contextlib.suppress(OSError):
                    conn.sendall(
                        (json.dumps({"ok": False, "error": str(e)}) + "\n").encode("utf-8")
                    )
            finally:
                conn.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    yield sock_path, thread
    server.close()
    if sock_path.exists():
        sock_path.unlink()


# ─────────────────────────────────────────────────────────────────────
# DaemonClient — round-trip with a mock server
# ─────────────────────────────────────────────────────────────────────


class TestDaemonClient:
    def test_is_running_when_socket_exists(
        self, mock_daemon: tuple[Path, threading.Thread],
    ) -> None:
        sock_path, _ = mock_daemon
        client = DaemonClient(sock_path)
        assert client.is_running()

    def test_is_running_false_when_socket_missing(
        self, tmp_path: Path,
    ) -> None:
        client = DaemonClient(tmp_path / "no-such.sock")
        assert client.is_running() is False

    def test_ping_returns_metadata(
        self, mock_daemon: tuple[Path, threading.Thread],
    ) -> None:
        sock_path, _ = mock_daemon
        client = DaemonClient(sock_path)
        info = client.ping()
        assert info is not None
        assert info["ok"] is True
        assert info["model_path"] == "/mock.gguf"
        assert "model_hash" in info

    def test_ping_returns_none_when_not_running(self, tmp_path: Path) -> None:
        client = DaemonClient(tmp_path / "no-such.sock")
        assert client.ping() is None

    def test_evaluate_block_for_rm_rf(
        self, mock_daemon: tuple[Path, threading.Thread],
    ) -> None:
        sock_path, _ = mock_daemon
        client = DaemonClient(sock_path)
        resp = client.evaluate(
            'tool=Bash command="rm -rf /var/log"', {"tool_arg_inspection": 0.95},
        )
        assert resp is not None
        assert resp.decision == "BLOCK"
        assert resp.confidence == 0.9
        assert "mock daemon" in resp.reason

    def test_evaluate_allow_for_benign(
        self, mock_daemon: tuple[Path, threading.Thread],
    ) -> None:
        sock_path, _ = mock_daemon
        client = DaemonClient(sock_path)
        resp = client.evaluate(
            'tool=Bash command="ls"', {"tool_arg_inspection": 0.10},
        )
        assert resp is not None
        assert resp.decision == "ALLOW"

    def test_evaluate_returns_none_when_not_running(
        self, tmp_path: Path,
    ) -> None:
        """The client must NEVER raise — caller falls back to in-process."""
        client = DaemonClient(tmp_path / "no-such.sock")
        resp = client.evaluate("anything", {})
        assert resp is None

    def test_evaluate_returns_none_on_connection_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Stale socket file (no listener) → graceful None."""
        sock_path = tmp_path / "stale.sock"
        # Create the file but don't bind a listener — connect will refuse.
        sock_path.touch()
        client = DaemonClient(sock_path)
        # is_running checks is_socket() which is False for a regular file
        # → returns None before even attempting to connect.
        resp = client.evaluate("hi", {})
        assert resp is None

    def test_rag_block_round_trips(
        self, mock_daemon: tuple[Path, threading.Thread],
    ) -> None:
        sock_path, _ = mock_daemon
        client = DaemonClient(sock_path)
        resp = client.evaluate(
            "summary text",
            {"tool_arg_inspection": 0.5},
            rag_block="Similar past cases:\n- [cos=0.99] ...",
        )
        # Mock daemon doesn't care about rag_block content but the wire
        # protocol must accept it without error.
        assert resp is not None


# ─────────────────────────────────────────────────────────────────────
# PID file helpers
# ─────────────────────────────────────────────────────────────────────


class TestPidFile:
    def test_read_pid_file_missing_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AEGIS_SIDECAR_PID", str(tmp_path / "nope.pid"))
        assert read_pid_file() is None

    def test_read_pid_file_malformed_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        pid_path = tmp_path / "bad.pid"
        pid_path.write_text("not valid json")
        monkeypatch.setenv("AEGIS_SIDECAR_PID", str(pid_path))
        assert read_pid_file() is None

    def test_read_pid_file_returns_dict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        pid_path = tmp_path / "good.pid"
        pid_path.write_text(json.dumps({
            "pid": 12345, "model_path": "/foo.gguf",
        }))
        monkeypatch.setenv("AEGIS_SIDECAR_PID", str(pid_path))
        info = read_pid_file()
        assert info is not None
        assert info["pid"] == 12345
        assert info["model_path"] == "/foo.gguf"

    def test_is_pid_alive_for_self(self) -> None:
        assert is_pid_alive(os.getpid()) is True

    def test_is_pid_alive_for_invalid(self) -> None:
        # PID 0 is the kernel; PID 999999 is unlikely to exist.
        assert is_pid_alive(0) is False
        assert is_pid_alive(999999) is False


# ─────────────────────────────────────────────────────────────────────
# CLI argparse — sidecar subcommand
# ─────────────────────────────────────────────────────────────────────


class TestSidecarCli:
    def test_sidecar_subcommand_dispatches(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import aegis_cli

        for action in ("start", "stop", "status"):
            args = aegis_cli.build_parser().parse_args(["sidecar", action])
            assert args.action == action
            assert args.fn is aegis_cli.cmd_sidecar

    def test_sidecar_start_accepts_model_flag(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import aegis_cli

        args = aegis_cli.build_parser().parse_args(
            ["sidecar", "start", "--model", "/path/to/foo.gguf"]
        )
        assert args.model == "/path/to/foo.gguf"

    def test_sidecar_invalid_action_rejected(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import aegis_cli

        with pytest.raises(SystemExit):
            aegis_cli.build_parser().parse_args(["sidecar", "wat"])


# ─────────────────────────────────────────────────────────────────────
# LocalPhiJudge fast-path (unit-level — uses mock daemon)
# ─────────────────────────────────────────────────────────────────────


class TestFastPath:
    def test_evaluate_full_uses_daemon_when_available(
        self,
        mock_daemon: tuple[Path, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the daemon is reachable, evaluate_full bypasses
        in-process model load and returns the daemon's verdict."""
        from aegis.atv.builder import build_atv
        from aegis.judge.local_phi import LocalPhiJudge
        from aegis.schema import (
            ATVHeader,
            ATVInput,
            CostEfficiencyMetrics,
        )

        # No real GGUF — would force stub mode without daemon.
        monkeypatch.delenv("AEGIS_JUDGE_MODEL_PATH", raising=False)
        monkeypatch.delenv("AEGIS_JUDGE_LOCAL_PHI_STUB", raising=False)

        inp = ATVInput(
            header=ATVHeader(
                trace_id="t", span_id="s", tenant_id="t", aid="a",
                timestamp_ns=time.time_ns(),
            ),
            agent_state_text="x", plan_text="x",
            tool_name="Bash",
            tool_args_json='{"command":"rm -rf /var/log"}',
            safety_flags={}, memory_fingerprint="sha3:t",
            cost_estimate=CostEfficiencyMetrics(
                input_token_count=1, output_token_count=1,
            ),
        )
        atv = build_atv(inp)
        verdict = LocalPhiJudge().evaluate_full(
            'tool=Bash command="rm -rf /var/log"', atv=atv, inp=inp,
        )
        # Mock daemon BLOCKs on rm-rf substring.
        assert verdict.decision == "BLOCK"
        assert "[daemon]" in verdict.reason

    def test_evaluate_full_falls_back_when_daemon_returns_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """If the daemon socket exists but every call fails, evaluate_full
        must NOT crash — falls back to the in-process / stub path."""
        # Create a stale socket file (regular file, not a real socket).
        sock_path = tmp_path / "stale.sock"
        sock_path.touch()
        monkeypatch.setenv("AEGIS_SIDECAR_SOCK", str(sock_path))
        monkeypatch.delenv("AEGIS_JUDGE_MODEL_PATH", raising=False)

        from aegis.atv.builder import build_atv
        from aegis.judge.local_phi import LocalPhiJudge
        from aegis.schema import (
            ATVHeader,
            ATVInput,
            CostEfficiencyMetrics,
        )

        inp = ATVInput(
            header=ATVHeader(
                trace_id="t", span_id="s", tenant_id="t", aid="a",
                timestamp_ns=time.time_ns(),
            ),
            agent_state_text="x", plan_text="x",
            tool_name="Bash", tool_args_json='{"command":"ls"}',
            safety_flags={}, memory_fingerprint="sha3:t",
            cost_estimate=CostEfficiencyMetrics(
                input_token_count=1, output_token_count=1,
            ),
        )
        atv = build_atv(inp)
        # Should NOT raise; falls back to stub mode (no GGUF env, no daemon).
        verdict = LocalPhiJudge().evaluate_full(
            'tool=Bash command="ls"', atv=atv, inp=inp,
        )
        assert verdict.decision in {"ALLOW", "BLOCK", "REQUIRE_APPROVAL"}
        # Reason should NOT contain the daemon marker (we fell through).
        assert "[daemon]" not in verdict.reason
