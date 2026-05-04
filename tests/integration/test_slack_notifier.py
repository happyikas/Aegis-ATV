"""SlackWebhookNotifier + CompositeNotifier tests.

Three layers, in order of how much real I/O they exercise:

1. **Unit (urlopen mock)** — patches ``urllib.request.urlopen`` so no
   sockets open. Verifies wire format, headers, decision policy,
   error swallowing, and the public success/failure counters.

2. **Integration (local HTTP server)** — spins up a real
   ``http.server.ThreadingHTTPServer`` on localhost:0, points the
   notifier at it, and asserts the server received the exact JSON
   the test expected. Catches any urllib mistake the mock layer
   would mask.

3. **E2E (multi-agent + Slack)** — runs the 5-agent fleet replay
   with a SlackWebhookNotifier wired up. Verifies the notifier is
   called once per fleet-threshold crossing with correct payload,
   and CompositeNotifier (Slack + Recording) preserves both
   broadcast + assertion paths.
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
from aegis.cost.multi_agent import (
    AgentReplayInput,
    FleetThreshold,
    RecordingNotifier,
    multi_agent_replay,
)
from aegis.cost.replay import ReplayConfig
from aegis.cost.slack_notifier import SlackWebhookNotifier

# ─────────────────────────────────────────────────────────────────────
# 1. Unit tests — urlopen mock
# ─────────────────────────────────────────────────────────────────────


class TestSlackUnitMock:
    """Verify wire format and decision policy without opening sockets."""

    def _mock_urlopen(self, *, status: int = 200) -> MagicMock:
        """Build a mock urlopen that returns a 200 OK by default."""
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.status = status
        cm.read = MagicMock(return_value=b"ok")
        return cm

    def test_constructor_rejects_empty_url(self) -> None:
        with pytest.raises(ValueError, match="webhook_url"):
            SlackWebhookNotifier("")

    def test_warn_crossing_posts_and_continues(self) -> None:
        n = SlackWebhookNotifier(
            "https://hooks.slack.com/services/X/Y/Z",
        )
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen()) as mu:
            decision = n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=5.0, label="warn"),
                fleet_dollars=5.42, aid="agent-2", call_idx=7,
            )
        assert decision == "continue"
        assert n.n_posts_attempted == 1
        assert n.n_posts_succeeded == 1
        assert n.n_posts_failed == 0
        # Inspect the request that urlopen received.
        req = mu.call_args.args[0]
        assert req.get_method() == "POST"
        assert req.full_url == "https://hooks.slack.com/services/X/Y/Z"
        assert req.headers["Content-type"] == "application/json"
        body = json.loads(req.data.decode("utf-8"))
        assert "fleet cost" in body["text"]
        assert "$5.4200" in body["text"]
        assert "agent-2" in body["text"]
        assert "warn" in body["text"]
        assert "⚠️" in body["text"]

    def test_hard_stop_posts_and_aborts(self) -> None:
        n = SlackWebhookNotifier("https://hooks.slack.com/services/X/Y/Z")
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen()):
            decision = n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=10.0, label="hard_stop"),
                fleet_dollars=10.5, aid="a4", call_idx=12,
            )
        assert decision == "abort"
        assert n.n_posts_succeeded == 1

    def test_http_5xx_marks_failure_but_keeps_continue(self) -> None:
        n = SlackWebhookNotifier(
            "https://hooks.slack.com/services/X/Y/Z",
            record_failures=False,   # silence stderr noise in tests
        )
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_urlopen(status=503),
        ):
            decision = n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=5.0, label="warn"),
                fleet_dollars=5.5, aid="a", call_idx=0,
            )
        # Decision still respects policy even when webhook fails.
        assert decision == "continue"
        assert n.n_posts_attempted == 1
        assert n.n_posts_succeeded == 0
        assert n.n_posts_failed == 1

    def test_url_error_is_swallowed(self) -> None:
        n = SlackWebhookNotifier(
            "https://hooks.slack.com/services/X/Y/Z",
            record_failures=False,
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            decision = n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=5.0, label="warn"),
                fleet_dollars=5.5, aid="a", call_idx=0,
            )
        assert decision == "continue"
        assert n.n_posts_failed == 1

    def test_timeout_is_swallowed(self) -> None:
        n = SlackWebhookNotifier(
            "https://hooks.slack.com/services/X/Y/Z",
            record_failures=False,
        )
        with patch(
            "urllib.request.urlopen", side_effect=TimeoutError("slow"),
        ):
            decision = n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=10.0, label="hard_stop"),
                fleet_dollars=11.0, aid="a", call_idx=0,
            )
        # hard_stop policy still aborts even if Slack timed out.
        assert decision == "abort"
        assert n.n_posts_failed == 1

    def test_custom_decision_policy(self) -> None:
        """Operator can override the policy — e.g. always-continue
        because Slack alone shouldn't have authority to stop."""
        n = SlackWebhookNotifier(
            "https://hooks.slack.com/services/X/Y/Z",
            decision_policy=lambda _t: "continue",
        )
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen()):
            decision = n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=10.0, label="hard_stop"),
                fleet_dollars=11.0, aid="a", call_idx=0,
            )
        assert decision == "continue"

    def test_custom_format_payload(self) -> None:
        """Operator can use a different Slack payload shape (blocks,
        attachments, mentions, etc.)."""
        def fancy(*, threshold, fleet_dollars, aid, call_idx):
            return {
                "text": f"@oncall fleet>${threshold.dollars}",
                "blocks": [{"type": "section"}],
            }
        n = SlackWebhookNotifier(
            "https://hooks.slack.com/services/X/Y/Z",
            format_payload=fancy,
        )
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen()) as mu:
            n.on_threshold_crossing(
                threshold=FleetThreshold(dollars=5.0, label="warn"),
                fleet_dollars=5.5, aid="a", call_idx=0,
            )
        body = json.loads(mu.call_args.args[0].data.decode("utf-8"))
        assert body["text"].startswith("@oncall")
        assert "blocks" in body


# ─────────────────────────────────────────────────────────────────────
# 2. Integration tests — real local HTTP server
# ─────────────────────────────────────────────────────────────────────


class _CollectingHandler(BaseHTTPRequestHandler):
    """Stash every POST body on the server instance."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        # Server is a bare ThreadingHTTPServer; we attach `received`
        # as an attribute below.
        getattr(self.server, "received", []).append(
            {
                "path": self.path,
                "headers": {k: v for k, v in self.headers.items()},
                "body": body,
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: Any) -> None:
        pass  # suppress test-noise stderr


@pytest.fixture
def fake_slack() -> tuple[str, list[dict[str, Any]], threading.Thread, ThreadingHTTPServer]:
    """Spin up a real localhost HTTP server. Yields the URL and a
    list that accumulates received requests."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CollectingHandler)
    received: list[dict[str, Any]] = []
    server.received = received  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    url = f"http://{host}:{port}/services/X/Y/Z"
    yield url, received, thread, server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2.0)


class TestSlackIntegrationLocal:
    """Real socket. Catches anything the urlopen mock would miss."""

    def test_real_post_arrives_with_correct_body(self, fake_slack) -> None:
        url, received, *_ = fake_slack
        n = SlackWebhookNotifier(url, timeout_s=2.0)
        decision = n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.50, aid="agent-3", call_idx=4,
        )
        assert decision == "continue"
        assert n.n_posts_succeeded == 1
        assert len(received) == 1
        req = received[0]
        assert req["path"] == "/services/X/Y/Z"
        assert req["headers"].get("Content-Type") == "application/json"
        payload = json.loads(req["body"].decode("utf-8"))
        assert "$5.5000" in payload["text"]
        assert "agent-3" in payload["text"]

    def test_unreachable_url_is_swallowed(self) -> None:
        # Port 1 is reliably refused on macOS / Linux.
        n = SlackWebhookNotifier(
            "http://127.0.0.1:1/refused",
            timeout_s=0.5,
            record_failures=False,
        )
        decision = n.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.5, aid="a", call_idx=0,
        )
        assert decision == "continue"   # policy still applies
        assert n.n_posts_attempted == 1
        assert n.n_posts_failed == 1


# ─────────────────────────────────────────────────────────────────────
# 3. CompositeNotifier
# ─────────────────────────────────────────────────────────────────────


class TestCompositeNotifier:
    def test_rejects_empty_list(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            CompositeNotifier([])

    def test_calls_every_sub_notifier(self) -> None:
        a = RecordingNotifier()
        b = RecordingNotifier()
        c = CompositeNotifier([a, b])
        c.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.5, aid="x", call_idx=0,
        )
        assert len(a.crossings) == 1
        assert len(b.crossings) == 1
        assert c.n_calls == 1

    def test_any_abort_makes_composite_abort(self) -> None:
        # `a` says continue, `b` says abort → composite aborts.
        a = RecordingNotifier(decisions=["continue"])
        b = RecordingNotifier(decisions=["abort"])
        c = CompositeNotifier([a, b])
        d = c.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.5, aid="x", call_idx=0,
        )
        assert d == "abort"
        assert c.n_aborted == 1

    def test_all_continue_means_continue(self) -> None:
        a = RecordingNotifier(decisions=["continue"])
        b = RecordingNotifier(decisions=["continue"])
        c = CompositeNotifier([a, b])
        assert c.on_threshold_crossing(
            threshold=FleetThreshold(dollars=5.0, label="warn"),
            fleet_dollars=5.5, aid="x", call_idx=0,
        ) == "continue"

    def test_slack_plus_recording_in_real_replay(
        self, tmp_path: Path, fake_slack
    ) -> None:
        """Production combo: Slack notifies the team, RecordingNotifier
        is the in-process audit. CompositeNotifier preserves both."""
        url, received, *_ = fake_slack

        # Synthesize 5 tiny agents that cross $5 mid-replay.
        from tests.integration.test_multi_agent_cost import (
            synth_agent_transcript,
        )
        agents = [
            AgentReplayInput(
                transcript_path=synth_agent_transcript(
                    tmp_path / f"a{i}.jsonl",
                    agent_idx=i,
                    n_turns=10,
                ),
                aid=f"agent-{i}",
            )
            for i in range(1, 6)
        ]
        slack = SlackWebhookNotifier(url, timeout_s=2.0)
        recorder = RecordingNotifier()
        composite = CompositeNotifier([slack, recorder])

        s = multi_agent_replay(
            agents,
            thresholds=[FleetThreshold(dollars=5.0, label="warn")],
            config_template=ReplayConfig(
                transcript_path=tmp_path / "ignored.jsonl",
                budget_dollars=100.0,
            ),
            notifier=composite,
        )
        # Both sub-notifiers saw the crossing.
        assert s.n_total_calls == 50
        assert len(recorder.crossings) == 1
        assert slack.n_posts_succeeded == 1
        # The fake server received exactly one POST with our text.
        assert len(received) == 1
        body = json.loads(received[0]["body"].decode("utf-8"))
        assert "fleet cost" in body["text"]
        assert "warn" in body["text"]
