"""NtfyNotifier + FileNotifier tests.

Same three-layer pattern as the Slack tests:

1. **Unit (urlopen mock)** for ntfy — verify wire format (POST,
   plain-text body, ntfy-specific headers), priority/tags mapping,
   error swallowing, custom format
2. **Integration (real local HTTP server)** for ntfy — full urllib
   path, fake server captures the body + headers
3. **FileNotifier unit + integration** — append correctness, JSONL
   shape, parent-dir auto-create, decision policy
4. **End-to-end** — `make_default_notifier` composing both into a
   single CompositeNotifier wired through `multi_agent_replay`
"""

from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from aegis.cost.composite_notifier import CompositeNotifier
from aegis.cost.file_notifier import FileNotifier
from aegis.cost.fleet_monitor import make_default_notifier
from aegis.cost.multi_agent import (
    AgentReplayInput,
    FleetThreshold,
    multi_agent_replay,
)
from aegis.cost.ntfy_notifier import NtfyNotifier
from aegis.cost.replay import ReplayConfig

# ─────────────────────────────────────────────────────────────────────
# 1. NtfyNotifier — urlopen mock
# ─────────────────────────────────────────────────────────────────────


class TestNtfyUnit:
    def _mock_urlopen(self, *, status: int = 200) -> MagicMock:
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.status = status
        cm.read = MagicMock(return_value=b"{}")
        return cm

    def test_constructor_rejects_empty_topic(self) -> None:
        with pytest.raises(ValueError, match="topic"):
            NtfyNotifier(topic="")

    def test_warn_sends_default_priority(self) -> None:
        n = NtfyNotifier(topic="aegis-test-warn")
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_urlopen(),
        ) as mu:
            decision = n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=5.0, label="warn"),
                fleet_dollars=5.42, aid="agent-1", call_idx=3,
            )
        assert decision == "continue"
        assert n.n_posts_succeeded == 1
        req = mu.call_args.args[0]
        assert req.full_url == "https://ntfy.sh/aegis-test-warn"
        assert req.get_method() == "POST"
        # ntfy-specific headers — note urllib normalises header case.
        assert req.headers["Title"].startswith("Aegis Fleet Cost")
        assert req.headers["Priority"] == "default"
        assert "warning" in req.headers["Tags"]
        # Body is plain text (not JSON).
        body = req.data.decode("utf-8")
        assert "$5.4200" in body
        assert "agent-1" in body

    def test_hard_stop_uses_high_priority_and_aborts(self) -> None:
        n = NtfyNotifier(topic="aegis-test-hard")
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_urlopen(),
        ) as mu:
            decision = n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=20.0, label="hard_stop"),
                fleet_dollars=21.0, aid="agent-4", call_idx=10,
            )
        assert decision == "abort"
        req = mu.call_args.args[0]
        assert req.headers["Priority"] == "high"
        assert "rotating_light" in req.headers["Tags"]

    def test_url_error_swallowed_decision_still_applied(self) -> None:
        n = NtfyNotifier(topic="t", record_failures=False)
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("dns fail"),
        ):
            d = n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=20.0, label="hard_stop"),
                fleet_dollars=21.0, aid="a", call_idx=0,
            )
        assert d == "abort"   # policy still applies
        assert n.n_posts_failed == 1

    def test_5xx_marks_failure(self) -> None:
        n = NtfyNotifier(topic="t", record_failures=False)
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_urlopen(status=503),
        ):
            n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=5.0, label="warn"),
                fleet_dollars=5.5, aid="a", call_idx=0,
            )
        assert n.n_posts_failed == 1
        assert n.n_posts_succeeded == 0

    def test_custom_base_url_for_self_hosted(self) -> None:
        n = NtfyNotifier(
            topic="my-topic", base_url="http://my-ntfy.internal:8080",
        )
        assert n.url == "http://my-ntfy.internal:8080/my-topic"

    def test_custom_format_payload(self) -> None:
        def fancy(*, threshold, fleet_dollars, aid, call_idx):
            return ("Custom Title", "Custom Body", "urgent", "fire")
        n = NtfyNotifier(topic="t", format_payload=fancy)
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_urlopen(),
        ) as mu:
            n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=5.0, label="warn"),
                fleet_dollars=5.5, aid="a", call_idx=0,
            )
        req = mu.call_args.args[0]
        assert req.headers["Title"] == "Custom Title"
        assert req.headers["Priority"] == "urgent"
        assert req.headers["Tags"] == "fire"
        assert req.data.decode("utf-8") == "Custom Body"


# ─────────────────────────────────────────────────────────────────────
# 2. NtfyNotifier — real local HTTP server
# ─────────────────────────────────────────────────────────────────────


class _CollectingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n) if n else b""
        getattr(self.server, "received", []).append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body.decode("utf-8"),
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def fake_ntfy():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CollectingHandler)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}", server.received  # type: ignore[attr-defined]
    server.shutdown()
    server.server_close()
    thread.join(timeout=2.0)


class TestNtfyIntegration:
    def test_real_post_arrives(self, fake_ntfy) -> None:
        base_url, received = fake_ntfy
        n = NtfyNotifier(topic="my-topic", base_url=base_url, timeout_s=2.0)
        n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.42, aid="agent-1", call_idx=3,
        )
        assert n.n_posts_succeeded == 1
        assert len(received) == 1
        assert received[0]["path"] == "/my-topic"
        assert received[0]["headers"]["Title"].startswith("Aegis Fleet Cost")
        assert received[0]["headers"]["Priority"] == "default"
        assert "$5.4200" in received[0]["body"]

    def test_unreachable_swallowed(self) -> None:
        n = NtfyNotifier(
            topic="t", base_url="http://127.0.0.1:1",
            timeout_s=0.5, record_failures=False,
        )
        d = n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.5, aid="a", call_idx=0,
        )
        assert d == "continue"
        assert n.n_posts_failed == 1


# ─────────────────────────────────────────────────────────────────────
# 3. FileNotifier
# ─────────────────────────────────────────────────────────────────────


class TestFileNotifier:
    def test_appends_one_line_per_crossing(self, tmp_path: Path) -> None:
        path = tmp_path / "crossings.jsonl"
        n = FileNotifier(path)
        n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.42, aid="a1", call_idx=3,
        )
        n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=20.0, label="hard_stop"),
            fleet_dollars=21.0, aid="a4", call_idx=10,
        )
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        rec0 = json.loads(lines[0])
        rec1 = json.loads(lines[1])
        assert rec0["label"] == "warn"
        assert rec0["fleet_dollars"] == pytest.approx(5.42)
        assert rec0["aid_at_crossing"] == "a1"
        assert rec0["decision"] == "continue"
        assert rec1["label"] == "hard_stop"
        assert rec1["decision"] == "abort"

    def test_parent_dir_auto_created(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "crossings.jsonl"
        n = FileNotifier(path)
        n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.5, aid="a", call_idx=0,
        )
        assert path.is_file()

    def test_default_decision_policy(self, tmp_path: Path) -> None:
        n = FileNotifier(tmp_path / "x.jsonl")
        d_warn = n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.5, aid="a", call_idx=0,
        )
        d_hard = n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=20.0, label="hard_stop"),
            fleet_dollars=21.0, aid="a", call_idx=1,
        )
        assert d_warn == "continue"
        assert d_hard == "abort"

    def test_custom_decision_policy(self, tmp_path: Path) -> None:
        n = FileNotifier(
            tmp_path / "x.jsonl",
            decision_policy=lambda _t: "continue",
        )
        d = n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=20.0, label="hard_stop"),
            fleet_dollars=21.0, aid="a", call_idx=0,
        )
        assert d == "continue"

    def test_counters(self, tmp_path: Path) -> None:
        n = FileNotifier(tmp_path / "x.jsonl")
        for i in range(3):
            n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=1.0, label="warn"),
                fleet_dollars=1.5, aid=f"a{i}", call_idx=i,
            )
        assert n.n_writes_attempted == 3
        assert n.n_writes_succeeded == 3
        assert n.n_writes_failed == 0


# ─────────────────────────────────────────────────────────────────────
# 4. make_default_notifier composition
# ─────────────────────────────────────────────────────────────────────


class TestMakeDefaultNotifier:
    def test_no_options_returns_stderr_only(self) -> None:
        from aegis.cost.multi_agent import StderrNotifier
        n = make_default_notifier()
        assert isinstance(n, StderrNotifier)

    def test_ntfy_only_returns_composite(self) -> None:
        n = make_default_notifier(ntfy_topic="my-topic")
        assert isinstance(n, CompositeNotifier)
        # 2 sub-notifiers: stderr + ntfy
        assert len(n._notifiers) == 2

    def test_file_only_returns_composite(self, tmp_path: Path) -> None:
        n = make_default_notifier(crossings_log=str(tmp_path / "x.jsonl"))
        assert isinstance(n, CompositeNotifier)
        assert len(n._notifiers) == 2

    def test_ntfy_plus_file_returns_three(self, tmp_path: Path) -> None:
        n = make_default_notifier(
            ntfy_topic="t",
            crossings_log=str(tmp_path / "x.jsonl"),
        )
        assert isinstance(n, CompositeNotifier)
        # stderr + ntfy + file
        assert len(n._notifiers) == 3


# ─────────────────────────────────────────────────────────────────────
# 5. End-to-end: ntfy + file combo through multi_agent_replay
# ─────────────────────────────────────────────────────────────────────


class TestEndToEndNtfyFileCombo:
    def test_5_agent_burst_writes_to_file_and_posts_to_ntfy(
        self, tmp_path: Path, fake_ntfy
    ) -> None:
        base_url, received = fake_ntfy
        crossings_log = tmp_path / "crossings.jsonl"

        # 5 mini-agents → ~$0.10 fleet, threshold $0.05 fires.
        from tests.integration.test_multi_agent_cost import (
            synth_agent_transcript,
        )
        agents = [
            AgentReplayInput(
                transcript_path=synth_agent_transcript(
                    tmp_path / f"a{i}.jsonl",
                    agent_idx=i,
                    n_turns=2,
                    in_per_turn=200, out_per_turn=200,
                ),
                aid=f"agent-{i}",
            )
            for i in range(1, 6)
        ]

        notifier = CompositeNotifier([
            NtfyNotifier(topic="aegis-e2e-test", base_url=base_url, timeout_s=2.0),
            FileNotifier(crossings_log),
        ])
        s = multi_agent_replay(
            agents,
            thresholds=[FleetThreshold(dollars=0.001, label="warn")],
            config_template=ReplayConfig(
                transcript_path=tmp_path / "ignored.jsonl",
                budget_dollars=10.0,
            ),
            notifier=notifier,
        )

        # Both channels saw the crossing.
        assert len(s.crossings) >= 1
        assert len(received) >= 1
        # ntfy received it.
        assert received[0]["path"] == "/aegis-e2e-test"
        assert "$0.0" in received[0]["body"] or "$" in received[0]["body"]
        # File logged it.
        lines = crossings_log.read_text().strip().splitlines()
        assert len(lines) >= 1
        rec = json.loads(lines[0])
        assert rec["label"] == "warn"
        assert "fleet_dollars" in rec
