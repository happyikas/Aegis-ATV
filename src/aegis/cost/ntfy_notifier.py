"""ntfy.sh notifier — push fleet-cost crossings to a phone (or
browser) without a Slack workspace, signup, or API key.

ntfy.sh is a free public push-notification relay. The wire format
is dead simple — POST plain text (or JSON) to
``https://ntfy.sh/<your-topic>``, and any device subscribed to that
topic gets the message. The topic name doubles as a shared secret
(anyone who knows it can publish AND subscribe), so pick something
unguessable (``aegis-cost-alerts-<uuid>``).

For private use you can also point this at a self-hosted ntfy
server with ``base_url``.

Why we like it for fleet-cost alerts
------------------------------------

* Mobile push (iOS / Android free apps) — alerts arrive on the
  lock screen even when you're away from the desk
* No Slack workspace / Discord server needed
* No signup, no email, no rate limits at hobby scale
* Same Notifier protocol as Slack/Stderr, so it composes via
  :class:`aegis.cost.composite_notifier.CompositeNotifier`

Stdlib-only — no new project deps. Failure-isolated (timeout, DNS,
4xx, 5xx all silently dropped to stderr) so a flaky push service
never crashes a fleet replay.

Reference: https://docs.ntfy.sh/publish/
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from collections.abc import Callable

from aegis.cost.multi_agent import Decision, FleetThreshold

DEFAULT_BASE_URL: str = "https://ntfy.sh"
DEFAULT_TIMEOUT_S: float = 5.0
DEFAULT_USER_AGENT: str = "Aegis-ATV-cost-notifier/1.0 (ntfy)"


def _default_format(
    *,
    threshold: FleetThreshold,
    fleet_dollars: float,
    aid: str,
    call_idx: int,
) -> tuple[str, str, str, str]:
    """Default ntfy payload — returns (title, body, priority, tags).

    Maps threshold label to ntfy priority:
      warn       → "default"  (silent on lock screen, banner only)
      hard_stop  → "high"     (sound + vibrate)
    """
    # ASCII-only title — urllib HTTP headers are latin-1 by default,
    # so any non-ASCII char (em-dash, emoji) raises UnicodeEncodeError
    # at request time. Body is plain text and CAN carry unicode.
    title = f"Aegis Fleet Cost - {threshold.label.upper()}"
    body = (
        f"${fleet_dollars:.4f} crossed ${threshold.dollars:.4f} "
        f"(agent={aid}, call#{call_idx})"
    )
    priority = "high" if threshold.label == "hard_stop" else "default"
    tags = "rotating_light,money" if threshold.label == "hard_stop" else "warning,money"
    return title, body, priority, tags


class NtfyNotifier:
    """Multi-agent fleet notifier that POSTs crossings to ntfy.sh.

    Constructor parameters
    ----------------------
    topic : str
        The ntfy topic name. Public ntfy.sh treats this as a shared
        secret — pick something unguessable. Use ``base_url`` to
        point at a private / self-hosted server.
    base_url : str, default "https://ntfy.sh"
        ntfy server endpoint.
    timeout_s : float, default 5.0
        urlopen timeout.
    decision_policy : Callable, optional
        ``(threshold) -> Decision``. Defaults to ``warn → continue``,
        ``hard_stop → abort``.
    format_payload : Callable, optional
        ``(threshold, fleet_dollars, aid, call_idx) ->
        (title, body, priority, tags)``. Override for custom
        message formatting.
    record_failures : bool, default True
        Write webhook errors to stderr (set False for silent ops).
    """

    def __init__(
        self,
        *,
        topic: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        decision_policy: Callable[[FleetThreshold], Decision] | None = None,
        format_payload: (
            Callable[..., tuple[str, str, str, str]] | None
        ) = None,
        record_failures: bool = True,
    ) -> None:
        if not topic:
            raise ValueError("topic must be a non-empty string")
        self.topic = topic
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self._decision_policy = decision_policy or _default_decision_policy
        self._format_payload = format_payload or _default_format
        self.record_failures = bool(record_failures)
        self.n_posts_attempted: int = 0
        self.n_posts_succeeded: int = 0
        self.n_posts_failed: int = 0

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.topic}"

    def on_threshold_crossing(
        self,
        *,
        threshold: FleetThreshold,
        fleet_dollars: float,
        aid: str,
        call_idx: int,
    ) -> Decision:
        title, body, priority, tags = self._format_payload(
            threshold=threshold,
            fleet_dollars=fleet_dollars,
            aid=aid,
            call_idx=call_idx,
        )
        self._post(body=body, title=title, priority=priority, tags=tags)
        return self._decision_policy(threshold)

    def _post(
        self,
        *,
        body: str,
        title: str,
        priority: str,
        tags: str,
    ) -> None:
        """Best-effort POST. Errors → stderr (or silent)."""
        self.n_posts_attempted += 1
        try:
            req = urllib.request.Request(
                url=self.url,
                data=body.encode("utf-8"),
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Title": title,
                    "Priority": priority,
                    "Tags": tags,
                    "User-Agent": DEFAULT_USER_AGENT,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = getattr(resp, "status", 200)
                if 200 <= int(status) < 300:
                    self.n_posts_succeeded += 1
                    return
                self.n_posts_failed += 1
                if self.record_failures:
                    sys.stderr.write(
                        f"[ntfy-notifier] returned status={status}\n"
                    )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self.n_posts_failed += 1
            if self.record_failures:
                sys.stderr.write(f"[ntfy-notifier] POST failed: {e!r}\n")
        except Exception as e:  # noqa: BLE001 — never crash on telemetry
            self.n_posts_failed += 1
            if self.record_failures:
                sys.stderr.write(f"[ntfy-notifier] unexpected error: {e!r}\n")


def _default_decision_policy(threshold: FleetThreshold) -> Decision:
    """warn → continue, hard_stop → abort. Same as StderrNotifier
    and SlackWebhookNotifier."""
    return "abort" if threshold.label == "hard_stop" else "continue"
