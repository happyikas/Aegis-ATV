"""OpenRouter adapter — canonical ``provider`` string + fallback
chain extraction.

OpenRouter (https://openrouter.ai) is an LLM gateway that fronts
60+ inference providers and 300+ models behind a single OpenAI-
compatible API. Aegis cares about OpenRouter for one specific
reason: when a tool call's LLM intent originated from an OpenRouter
request, the ``provider`` field on the ATV header should reflect
**which provider actually served the request**, not the user-facing
``openrouter`` label. Otherwise ``aegis report --by-provider``
collapses every OpenRouter call into one bucket, defeating the
cross-provider drift advisor.

OpenRouter exposes the served provider in two places:

1. The ``provider_responses`` array in the response body — one entry
   per attempt during routing / fallback. The successful provider is
   the **last** entry with HTTP < 400 (the chain stops at success).

2. Some SDKs surface the ``x-openrouter-provider`` response header,
   though that's not consistently documented. We support it as a
   secondary signal but the body field is the canonical source.

Canonical string format
-----------------------
Aegis's existing ``provider`` strings follow ``<vendor>-<model>``
(e.g. ``anthropic-claude-3-5``, ``openai-gpt-4o``). When the call
goes through OpenRouter we prefix with ``openrouter:`` so users can
filter "all OpenRouter routes" with a single grep::

    openrouter:anthropic-claude-sonnet-4
    openrouter:openai-gpt-4o-mini
    openrouter:deepinfra-llama-3.3-70b-instruct

The model component is the slug component after the last ``/`` in
the OpenRouter ``model`` field. The vendor component is the
lower-cased name of the **actually-served** provider from
``provider_responses[-1]`` (or, when that array is absent, the
request slug's prefix component as a best-effort fallback).

Wiring
------
The helper is a pure function. Callers (Python agents, OpenClaw
plugin bridges, custom wrappers) call it after an OpenRouter request
completes and pass the returned string to Aegis::

    from aegis.integrations.openrouter import canonical_provider

    response = openrouter_client.chat.completions.create(
        model="anthropic/claude-sonnet-4",
        messages=[...],
    )
    provider = canonical_provider(response_dict_or_obj)
    # → "openrouter:anthropic-claude-sonnet-4"

    aegis_evaluate(..., header=ATVHeader(..., provider=provider))

Privacy
-------
We never store request/response bodies — only the model slug + the
ordered list of attempted-provider names. Aegis's audit chain treats
``provider`` as metadata; it's not sensitive content.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# ── public types ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderAttempt:
    """One attempt within an OpenRouter fallback chain."""

    name: str
    """Provider name as OpenRouter reports it (e.g. ``Anthropic``).
    Preserve original case; consumers can normalise."""

    http_status: int
    """HTTP status code of this attempt (e.g. 200 / 503 / 429).
    OpenRouter advances to the next provider on >= 400."""

    @property
    def is_success(self) -> bool:
        """HTTP 2xx is the only success class."""
        return 200 <= self.http_status < 300


@dataclass(frozen=True)
class OpenRouterCall:
    """Structured summary of one OpenRouter request — what Aegis
    needs to reason about the route, NOT what the model produced.

    Returned by :func:`parse_response`. Callers usually consume
    :attr:`provider_string` (the canonical ``provider`` value for
    ATV) but may keep the full attempt chain for cost / drift
    reporting.
    """

    requested_model: str
    """The OpenRouter ``model`` slug from the request, e.g.
    ``anthropic/claude-sonnet-4``. Empty string if absent."""

    actual_provider: str
    """Name of the provider that successfully served the request
    (last attempt with HTTP < 400). Falls back to the request slug's
    vendor prefix when the response carries no ``provider_responses``
    array."""

    attempts: tuple[ProviderAttempt, ...]
    """Full fallback chain in the order OpenRouter tried them.
    Empty tuple when the gateway didn't report it."""

    @property
    def is_fallback(self) -> bool:
        """``True`` iff more than one attempt occurred — i.e. at
        least one provider failed before a later one succeeded.
        Useful for cost-divergence + reliability signals."""
        return len(self.attempts) > 1

    @property
    def model_slug(self) -> str:
        """The model component, after the last ``/`` of the
        requested model slug. E.g. ``claude-sonnet-4`` from
        ``anthropic/claude-sonnet-4``."""
        if not self.requested_model:
            return ""
        return self.requested_model.rsplit("/", 1)[-1]

    @property
    def provider_string(self) -> str:
        """Canonical Aegis ``provider`` value for this call.

        Format: ``openrouter:<vendor>-<model>`` (lowercase, hyphens).
        Empty model slug (no requested model recorded) yields
        ``openrouter:<vendor>``.
        """
        vendor = _slugify(self.actual_provider) or "unknown"
        model = self.model_slug.lower()
        if not model:
            return f"openrouter:{vendor}"
        return f"openrouter:{vendor}-{model}"


# ── public API ───────────────────────────────────────────────────


def canonical_provider(
    response: Mapping[str, Any] | Any,
    *,
    headers: Mapping[str, str] | None = None,
) -> str:
    """Return the canonical Aegis ``provider`` string for an
    OpenRouter response.

    Convenience wrapper around :func:`parse_response`. When you only
    need the provider string (the common case), call this; when you
    also need the fallback chain, call :func:`parse_response`.

    Parameters
    ----------
    response:
        The OpenRouter response. Accepts either a parsed dict (from
        ``response.json()``) or an SDK response object that has the
        standard attributes ``model`` and ``provider_responses``.
    headers:
        Optional response headers. We look for
        ``x-openrouter-provider`` as a secondary signal when the
        body lacks ``provider_responses``.

    Returns
    -------
    str
        The canonical ``provider`` value, e.g.
        ``"openrouter:anthropic-claude-sonnet-4"``. Always non-empty;
        returns ``"openrouter:unknown"`` when both response body and
        headers are silent.
    """
    return parse_response(response, headers=headers).provider_string


def parse_response(
    response: Mapping[str, Any] | Any,
    *,
    headers: Mapping[str, str] | None = None,
) -> OpenRouterCall:
    """Parse an OpenRouter response into a structured
    :class:`OpenRouterCall`.

    Robust to:
      * dict / SDK-object input
      * missing ``provider_responses`` (falls back to slug vendor
        or the ``x-openrouter-provider`` header)
      * all-failed fallback chain (uses the last attempt regardless
        of status, so consumers can still see what was tried)
      * malformed entries (skipped silently)
    """
    requested_model = _get_attr(response, "model") or ""
    raw_attempts = _get_attr(response, "provider_responses") or []
    attempts = _parse_attempts(raw_attempts)

    actual = _resolve_actual_provider(
        attempts=attempts,
        headers=headers,
        requested_model=requested_model,
    )

    return OpenRouterCall(
        requested_model=str(requested_model),
        actual_provider=actual,
        attempts=attempts,
    )


def provider_chain(call: OpenRouterCall) -> str:
    """Render the attempt chain as a compact human string.

    Used by the cost-divergence advisor when a fallback occurred —
    callers can include this in audit explanations::

        "AnthropicVertex(503) → Anthropic(200)"
        "OpenAI(200)"
        "(no chain reported)"

    Returns ``"(no chain reported)"`` when the OpenRouter response
    didn't include a ``provider_responses`` array.
    """
    if not call.attempts:
        return "(no chain reported)"
    parts = [f"{a.name}({a.http_status})" for a in call.attempts]
    return " → ".join(parts)


# ── internals ────────────────────────────────────────────────────


def _slugify(name: str) -> str:
    """Normalise a provider name to lowercase hyphen-form.

    Examples::

        "Anthropic"        → "anthropic"
        "AnthropicVertex"  → "anthropic-vertex"
        "DeepInfra"        → "deep-infra"
        "OpenAI"           → "openai"        (trailing acronym stays joined)
        "xAI"              → "xai"           (single-letter prefix + acronym)
        "OpenAIBackup"     → "openai-backup" (acronym ends mid-word)
        "GPT4Provider"     → "gpt4-provider" (digit-then-word boundary)
        "Together AI"      → "together-ai"   (whitespace → hyphen)
        "groq_cloud"       → "groq-cloud"    (underscore → hyphen)

    Rule (simple form): insert a hyphen before an uppercase letter
    iff (a) it isn't the first char AND (b) it's directly followed
    by a lowercase letter. This treats trailing acronyms ("OpenAI",
    "xAI") as one word but correctly splits when an acronym ends
    mid-name ("OpenAIBackup" → "OpenAI" + "Backup"). Then lowercase
    everything and normalise other separators.
    """
    if not name:
        return ""
    s = name.strip()
    out: list[str] = []
    for i, ch in enumerate(s):
        if (
            ch.isupper()
            and i > 0
            and i + 1 < len(s)
            and s[i + 1].islower()
        ):
            out.append("-")
        out.append(ch.lower())
    result = "".join(out)
    result = result.replace("_", "-").replace(" ", "-")
    while "--" in result:
        result = result.replace("--", "-")
    return result.strip("-")


def _get_attr(obj: Any, name: str) -> Any:
    """Get ``name`` from either a Mapping or a generic object."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _parse_attempts(raw: Any) -> tuple[ProviderAttempt, ...]:
    """Convert a raw ``provider_responses`` list (or empty) into a
    tuple of :class:`ProviderAttempt`. Skips malformed entries."""
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    out: list[ProviderAttempt] = []
    for entry in raw:
        name = _get_attr(entry, "name")
        status = _get_attr(entry, "http_status")
        if not isinstance(name, str) or not name:
            continue
        try:
            status_int = int(status) if status is not None else 0
        except (TypeError, ValueError):
            continue
        out.append(ProviderAttempt(name=name, http_status=status_int))
    return tuple(out)


def _resolve_actual_provider(
    *,
    attempts: tuple[ProviderAttempt, ...],
    headers: Mapping[str, str] | None,
    requested_model: str,
) -> str:
    """Choose the canonical ``actual_provider`` name.

    Priority:
      1. Last success in ``attempts`` (HTTP 2xx)
      2. Last attempt of any status (when all failed but we still
         want to record what was tried)
      3. ``x-openrouter-provider`` response header
      4. The vendor prefix of the requested model slug
      5. The string ``"unknown"``
    """
    for attempt in reversed(attempts):
        if attempt.is_success:
            return attempt.name
    if attempts:
        return attempts[-1].name
    if headers:
        for key in ("x-openrouter-provider", "X-OpenRouter-Provider"):
            v = headers.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    if "/" in requested_model:
        return requested_model.split("/", 1)[0]
    return "unknown"
