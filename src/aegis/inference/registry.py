"""Multi-endpoint inference registry for the OpenClaw + Local OSS LLM
release track.

Gap B from the multi-agent + multi-LLM cross-grouping review (issue
#145). The single-endpoint :func:`scrape_vllm_metrics` is fine for a
homogeneous deployment (one vLLM server, one agent), but breaks down
the moment an OpenClaw operator runs multiple agents each pointing at
a different inference backend:

  Agent A (Telegram bot)        → vLLM at  http://10.0.0.10:8000
  Agent B (Code reviewer)       → vLLM at  http://10.0.0.20:8000
  Agent C (Research assistant)  → cloud (no /metrics endpoint at all)
  Agent D (Internal QA)         → vLLM at  http://10.0.0.30:8000

The registry maps each ``aid`` to the inference backend it uses, so
``aegis metrics --all`` can scrape every configured endpoint in one
shot and produce per-agent telemetry. Cloud agents (no /metrics) are
recorded so the consumer (e.g. ``aegis report --by-aid-and-provider
--with-live``) can show "Agent C: provider=anthropic-claude /
KV-hit=N/A" instead of pretending it didn't see the agent at all.

Schema lives at ``~/.aegis/inference.toml`` by default; override with
``AEGIS_INFERENCE_REGISTRY``. Format documented inline below.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final


class InferenceRegistryError(ValueError):
    """Raised when an inference.toml file is malformed.

    Aegis treats a malformed registry as "no registry" — the caller
    falls back to the legacy single-endpoint surface. The error is
    surfaced to the user via stderr so they can fix it, but the
    runtime keeps going (a security tool must not refuse to boot
    over a config-file typo).
    """


# Provider tags. ``vllm`` is fully scrapeable; ``cloud`` is recorded
# for attribution but skipped by the scraper. ``ollama`` / ``tgi``
# are reserved for follow-up adapter PRs.
_KNOWN_PROVIDERS: Final[frozenset[str]] = frozenset(
    {"vllm", "cloud", "ollama", "tgi"},
)
DEFAULT_TIMEOUT_S: Final[float] = 2.0


@dataclass(frozen=True)
class EndpointConfig:
    """One row in the registry.

    Frozen so the registry can be passed across threads (Aegis Live
    runs scrapes from a worker pool). Immutability also makes the
    "endpoint disabled mid-scrape" race impossible.

    Attributes:
        aid: The agent's stable identifier — must match the ``aid``
            field on the ATV records produced by the OpenClaw plugin
            (or the Aegis local-mode hook). The cross-reference in
            ``aegis report --by-aid-and-provider --with-live`` joins
            on this field.
        provider: One of ``"vllm"`` / ``"cloud"`` / ``"ollama"`` /
            ``"tgi"``. Currently only ``vllm`` is scrapeable; the
            others are recorded for future adapter work.
        metrics_url: Required for scrapeable providers (``vllm`` etc),
            ignored for ``cloud``. Full URL including ``/metrics`` if
            you want a non-default mount point — otherwise the
            scraper appends ``/metrics`` to the base.
        provider_name: Optional human label for cloud providers
            (e.g. ``"anthropic-claude-3-5"``). Reported by
            ``aegis metrics --all`` so an operator can read the table
            without cross-referencing the audit chain.
        timeout_s: Per-endpoint scrape timeout override.
        enabled: Set to ``false`` to keep an endpoint in config (for
            documentation / quick re-enable) without scraping it.
    """

    aid: str
    provider: str
    metrics_url: str | None = None
    provider_name: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    enabled: bool = True

    def is_scrapeable(self) -> bool:
        """``True`` if this endpoint exposes a Prometheus /metrics
        surface that :func:`scrape_vllm_metrics` can consume."""
        return self.enabled and self.provider == "vllm" and bool(self.metrics_url)


@dataclass(frozen=True)
class InferenceRegistry:
    """All endpoints from the TOML file.

    Attributes:
        endpoints: List of :class:`EndpointConfig`, ordered by ``aid``
            ascending for stable iteration.
        defaults_timeout_s: Fallback timeout if an endpoint doesn't
            override.
        source_path: Where this registry was loaded from — surfaced
            in error messages.
    """

    endpoints: tuple[EndpointConfig, ...] = ()
    defaults_timeout_s: float = DEFAULT_TIMEOUT_S
    source_path: Path | None = None

    def is_empty(self) -> bool:
        return not self.endpoints

    def by_aid(self, aid: str) -> EndpointConfig | None:
        """O(N) lookup. Registry is small (≤ a few hundred endpoints
        per realistic OpenClaw deployment) so a dict isn't needed."""
        for ep in self.endpoints:
            if ep.aid == aid:
                return ep
        return None


def default_registry_path() -> Path:
    """Returns the registry path Aegis will read by default.

    Env override (``AEGIS_INFERENCE_REGISTRY``) wins so the test
    suite can inject a temp file. Otherwise ``~/.aegis/inference.toml``.
    """
    env = os.environ.get("AEGIS_INFERENCE_REGISTRY")
    if env:
        return Path(env)
    return Path.home() / ".aegis" / "inference.toml"


def load_registry(path: Path | None = None) -> InferenceRegistry:
    """Load and validate ``inference.toml`` at ``path``.

    If ``path`` is ``None``, uses :func:`default_registry_path`. If
    the file does not exist, returns an empty registry — callers
    treat that as "no per-aid endpoints configured" and fall back to
    the legacy single-endpoint surface.

    Raises :class:`InferenceRegistryError` on:
        * malformed TOML
        * unknown provider tag
        * scrapeable provider with no ``metrics_url``
        * negative ``timeout_s``
        * duplicate ``aid``

    Empty / missing file returns ``InferenceRegistry()`` (no error).
    """
    target = path or default_registry_path()

    if not target.exists():
        return InferenceRegistry(source_path=target)

    try:
        with target.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise InferenceRegistryError(
            f"malformed TOML in {target}: {e}"
        ) from e

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise InferenceRegistryError(
            f"{target}: [defaults] must be a table, got {type(defaults).__name__}"
        )
    defaults_timeout = float(defaults.get("timeout_s", DEFAULT_TIMEOUT_S))
    if defaults_timeout <= 0:
        raise InferenceRegistryError(
            f"{target}: defaults.timeout_s must be > 0, got {defaults_timeout}"
        )

    endpoints_raw = data.get("endpoints", {})
    if not isinstance(endpoints_raw, dict):
        raise InferenceRegistryError(
            f"{target}: [endpoints] must be a table of tables, got {type(endpoints_raw).__name__}"
        )

    seen_aids: set[str] = set()
    parsed: list[EndpointConfig] = []
    for aid, body in endpoints_raw.items():
        if not isinstance(body, dict):
            raise InferenceRegistryError(
                f"{target}: [endpoints.{aid}] must be a table"
            )
        provider = body.get("provider")
        if not isinstance(provider, str) or provider not in _KNOWN_PROVIDERS:
            raise InferenceRegistryError(
                f"{target}: [endpoints.{aid}] provider must be one of "
                f"{sorted(_KNOWN_PROVIDERS)}; got {provider!r}"
            )

        metrics_url = body.get("metrics_url")
        if metrics_url is not None and not isinstance(metrics_url, str):
            raise InferenceRegistryError(
                f"{target}: [endpoints.{aid}] metrics_url must be a string"
            )
        if provider == "vllm" and not metrics_url:
            raise InferenceRegistryError(
                f"{target}: [endpoints.{aid}] provider=vllm requires "
                "metrics_url"
            )

        provider_name = body.get("provider_name")
        if provider_name is not None and not isinstance(provider_name, str):
            raise InferenceRegistryError(
                f"{target}: [endpoints.{aid}] provider_name must be a string"
            )

        timeout_s = float(body.get("timeout_s", defaults_timeout))
        if timeout_s <= 0:
            raise InferenceRegistryError(
                f"{target}: [endpoints.{aid}] timeout_s must be > 0, "
                f"got {timeout_s}"
            )

        enabled = bool(body.get("enabled", True))

        if aid in seen_aids:
            raise InferenceRegistryError(
                f"{target}: duplicate [endpoints.{aid}]"
            )
        seen_aids.add(aid)

        parsed.append(
            EndpointConfig(
                aid=aid,
                provider=provider,
                metrics_url=metrics_url,
                provider_name=provider_name,
                timeout_s=timeout_s,
                enabled=enabled,
            )
        )

    parsed.sort(key=lambda e: e.aid)
    return InferenceRegistry(
        endpoints=tuple(parsed),
        defaults_timeout_s=defaults_timeout,
        source_path=target,
    )
