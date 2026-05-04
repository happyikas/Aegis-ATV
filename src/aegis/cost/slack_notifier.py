"""Slack webhook notifier for multi-agent fleet cost crossings.

Posts a single Slack-formatted message every time the fleet cumulative
crosses a :class:`aegis.cost.multi_agent.FleetThreshold`. Designed for
the production Slack Incoming Webhook contract::

    POST https://hooks.slack.com/services/T.../B.../...
    Content-Type: application/json
    Body: {"text": "..."}
    →  200 OK, body "ok"

Stdlib-only (``urllib.request``) — no new project dependencies, no
``requests`` install. Failure-isolated: every webhook error
(timeout, DNS, 4xx, 5xx, malformed response) is swallowed and
written to stderr so a flaky webhook never crashes a fleet replay.

Decision policy mirrors :class:`StderrNotifier`:

* ``warn``       → ``continue``
* ``hard_stop``  → ``abort``

Override by passing ``decision=...`` to the constructor when the
operator wants different behaviour (e.g. always-continue when Slack
is fire-and-forget and human approval comes later).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable

from aegis.cost.multi_agent import Decision, FleetThreshold

DEFAULT_TIMEOUT_S: float = 5.0
DEFAULT_USER_AGENT: str = "Aegis-ATV-cost-notifier/1.0"


def _default_format(
    *,
    threshold: FleetThreshold,
    fleet_dollars: float,
    aid: str,
    call_idx: int,
) -> dict[str, object]:
    """Default Slack payload — single ``text`` field with an emoji
    badge. Customise by passing a ``format_payload`` callable."""
    icon = "🚨" if threshold.label == "hard_stop" else "⚠️"
    return {
        "text": (
            f"{icon} *fleet cost* `${fleet_dollars:.4f}` crossed "
            f"`{threshold.label}` threshold `${threshold.dollars:.4f}` "
            f"at agent `{aid}` (fleet call #{call_idx})"
        ),
    }


class SlackWebhookNotifier:
    """Multi-agent fleet notifier that posts to a Slack Incoming Webhook.

    Constructor parameters
    ----------------------
    webhook_url : str
        The full ``https://hooks.slack.com/services/T.../B.../...`` URL.
        Stored on the instance — keep it out of logs / version control.
    timeout_s : float, default 5.0
        ``urllib.request.urlopen`` timeout. Slack Incoming Webhooks
        almost always answer in <500 ms so 5 s is generous.
    decision_policy : Callable, optional
        ``(threshold) -> Decision``. Defaults to ``warn → continue``,
        ``hard_stop → abort`` (matches StderrNotifier).
    format_payload : Callable, optional
        ``(threshold, fleet_dollars, aid, call_idx) -> dict``. Returns
        the JSON dict POSTed to Slack. Default: a single-line message
        with a ⚠️ / 🚨 badge.
    record_failures : bool, default True
        If True (default), webhook errors are written to stderr.
        Set False for completely silent operation.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        decision_policy: (
            Callable[[FleetThreshold], Decision] | None
        ) = None,
        format_payload: (
            Callable[..., dict[str, object]] | None
        ) = None,
        record_failures: bool = True,
    ) -> None:
        if not webhook_url:
            raise ValueError("webhook_url must be a non-empty Slack URL")
        self.webhook_url = webhook_url
        self.timeout_s = float(timeout_s)
        self._decision_policy = decision_policy or _default_decision_policy
        self._format_payload = format_payload or _default_format
        self.record_failures = bool(record_failures)
        # Public counters — useful for assertions in production audits
        # ("did Slack actually receive 17 alerts last week?").
        self.n_posts_attempted: int = 0
        self.n_posts_succeeded: int = 0
        self.n_posts_failed: int = 0

    def on_threshold_crossing(
        self,
        *,
        threshold: FleetThreshold,
        fleet_dollars: float,
        aid: str,
        call_idx: int,
    ) -> Decision:
        payload = self._format_payload(
            threshold=threshold,
            fleet_dollars=fleet_dollars,
            aid=aid,
            call_idx=call_idx,
        )
        self._post(payload)
        return self._decision_policy(threshold)

    def _post(self, payload: dict[str, object]) -> None:
        """Best-effort POST to the webhook. Errors → stderr (or silent)."""
        self.n_posts_attempted += 1
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url=self.webhook_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": DEFAULT_USER_AGENT,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                # Slack returns "ok" body on 200. Treat any 2xx as success.
                status = getattr(resp, "status", 200)
                if 200 <= int(status) < 300:
                    self.n_posts_succeeded += 1
                    return
                # Non-2xx → record + log.
                self.n_posts_failed += 1
                if self.record_failures:
                    sys.stderr.write(
                        f"[slack-notifier] webhook returned status={status}\n"
                    )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self.n_posts_failed += 1
            if self.record_failures:
                sys.stderr.write(
                    f"[slack-notifier] webhook POST failed: {e!r}\n"
                )
        except Exception as e:  # noqa: BLE001 — never crash on telemetry
            self.n_posts_failed += 1
            if self.record_failures:
                sys.stderr.write(
                    f"[slack-notifier] unexpected error: {e!r}\n"
                )


def _default_decision_policy(threshold: FleetThreshold) -> Decision:
    """Mirrors :class:`StderrNotifier`: warn → continue, hard_stop → abort."""
    return "abort" if threshold.label == "hard_stop" else "continue"
