"""Tests for ``aegis.integrations.openrouter`` — the canonical
``provider`` string + fallback-chain extractor.

The adapter is pure (no network); these tests are fully offline.
Covers:

* slugification edge cases (camelCase, underscore, "OpenAI" / "DeepInfra"
  style names)
* canonical_provider() happy path with dict + object input
* fallback-chain resolution (last success wins, then last-any, then
  header, then slug prefix, then "unknown")
* provider_string format invariants
* malformed input tolerance
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from aegis.integrations.openrouter import (
    OpenRouterCall,
    ProviderAttempt,
    _slugify,
    canonical_provider,
    parse_response,
    provider_chain,
)

# ── slugify ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ("Anthropic", "anthropic"),
        ("AnthropicVertex", "anthropic-vertex"),
        ("OpenAI", "openai"),
        ("DeepInfra", "deep-infra"),
        ("Together AI", "together-ai"),
        ("groq_cloud", "groq-cloud"),
        ("GPT4Provider", "gpt4-provider"),
        ("xAI", "xai"),  # trailing-acronym rule: single-letter prefix + AI
        ("  Anthropic  ", "anthropic"),
        ("", ""),
        ("a", "a"),
        ("AB", "ab"),  # all-caps stays together (no lower preceding)
        ("camelCase", "camel-case"),
    ],
)
def test_slugify_examples(inp: str, expected: str) -> None:
    assert _slugify(inp) == expected


# ── canonical_provider — body-based ─────────────────────────────


def test_canonical_provider_simple_dict() -> None:
    response = {
        "model": "anthropic/claude-sonnet-4",
        "provider_responses": [
            {"name": "Anthropic", "http_status": 200},
        ],
    }
    assert canonical_provider(response) == "openrouter:anthropic-claude-sonnet-4"


def test_canonical_provider_fallback_chain_last_success_wins() -> None:
    """AnthropicVertex 503 → Anthropic 200: the 200 is the canonical
    served provider, even though it's not the first attempt."""
    response = {
        "model": "anthropic/claude-sonnet-4",
        "provider_responses": [
            {"name": "AnthropicVertex", "http_status": 503},
            {"name": "Anthropic", "http_status": 200},
        ],
    }
    assert canonical_provider(response) == "openrouter:anthropic-claude-sonnet-4"


def test_canonical_provider_uses_last_success_not_last_overall() -> None:
    """If a later attempt failed and an earlier one succeeded (rare
    but possible per OpenRouter docs), the success wins."""
    response = {
        "model": "x/y",
        "provider_responses": [
            {"name": "First", "http_status": 200},
            {"name": "Second", "http_status": 500},
        ],
    }
    # The reversed scan finds "First" as the last (only) success.
    assert canonical_provider(response) == "openrouter:first-y"


def test_canonical_provider_all_failed_uses_last_attempt() -> None:
    """When every provider in the chain returned a non-2xx, we still
    record what was tried — use the final attempt's name."""
    response = {
        "model": "openai/gpt-4o",
        "provider_responses": [
            {"name": "OpenAI", "http_status": 429},
            {"name": "OpenAIBackup", "http_status": 503},
        ],
    }
    assert canonical_provider(response) == "openrouter:openai-backup-gpt-4o"


def test_canonical_provider_no_chain_falls_back_to_slug_vendor() -> None:
    """OpenRouter sometimes omits provider_responses (older SDKs or
    direct cache hits). Use the model slug's vendor prefix."""
    response = {
        "model": "deepseek/deepseek-chat",
    }
    assert canonical_provider(response) == "openrouter:deepseek-deepseek-chat"


def test_canonical_provider_no_chain_no_slash() -> None:
    """If the model has no slash AND no chain, vendor is unknown
    — string is well-formed but the vendor segment is the literal
    'unknown' fallback."""
    response = {"model": "some-bare-model"}
    assert canonical_provider(response) == "openrouter:unknown-some-bare-model"


def test_canonical_provider_completely_empty() -> None:
    """Empty response → unknown vendor + empty model = bare
    'openrouter:unknown'."""
    assert canonical_provider({}) == "openrouter:unknown"


def test_canonical_provider_header_fallback() -> None:
    """When provider_responses is absent and the slug is unhelpful
    (no vendor prefix), the x-openrouter-provider header wins."""
    response = {"model": "model-with-no-slash"}
    headers = {"x-openrouter-provider": "Anthropic"}
    assert canonical_provider(response, headers=headers) == (
        "openrouter:anthropic-model-with-no-slash"
    )


def test_canonical_provider_header_case_variants() -> None:
    """Both lowercase and mixed-case header keys are accepted."""
    response = {"model": "x"}
    assert canonical_provider(
        response, headers={"X-OpenRouter-Provider": "OpenAI"}
    ) == "openrouter:openai-x"


def test_canonical_provider_body_overrides_header() -> None:
    """If the body has provider_responses, the header is ignored
    (body is canonical per OpenRouter docs)."""
    response = {
        "model": "x/y",
        "provider_responses": [{"name": "BodyWins", "http_status": 200}],
    }
    result = canonical_provider(
        response, headers={"x-openrouter-provider": "HeaderLoses"}
    )
    assert "body-wins" in result
    assert "header-loses" not in result


# ── object-style input ───────────────────────────────────────────


def test_canonical_provider_object_input() -> None:
    """SDK-style objects with .model and .provider_responses
    attributes should work identically to dict input."""

    @dataclass
    class FakeAttempt:
        name: str
        http_status: int

    @dataclass
    class FakeResponse:
        model: str
        provider_responses: list[Any]

    resp = FakeResponse(
        model="anthropic/claude-sonnet-4",
        provider_responses=[FakeAttempt("Anthropic", 200)],
    )
    assert canonical_provider(resp) == "openrouter:anthropic-claude-sonnet-4"


# ── parse_response — full structured output ──────────────────────


def test_parse_response_full_chain() -> None:
    response = {
        "model": "anthropic/claude-sonnet-4",
        "provider_responses": [
            {"name": "AnthropicVertex", "http_status": 503},
            {"name": "Anthropic", "http_status": 200},
        ],
    }
    call = parse_response(response)
    assert isinstance(call, OpenRouterCall)
    assert call.requested_model == "anthropic/claude-sonnet-4"
    assert call.actual_provider == "Anthropic"
    assert call.model_slug == "claude-sonnet-4"
    assert call.is_fallback is True
    assert len(call.attempts) == 2
    assert call.attempts[0].name == "AnthropicVertex"
    assert call.attempts[0].http_status == 503
    assert call.attempts[0].is_success is False
    assert call.attempts[1].is_success is True
    assert call.provider_string == "openrouter:anthropic-claude-sonnet-4"


def test_parse_response_single_attempt_not_fallback() -> None:
    response = {
        "model": "openai/gpt-4o",
        "provider_responses": [{"name": "OpenAI", "http_status": 200}],
    }
    call = parse_response(response)
    assert call.is_fallback is False
    assert len(call.attempts) == 1


def test_parse_response_no_chain_no_fallback() -> None:
    call = parse_response({"model": "x/y"})
    assert call.is_fallback is False
    assert call.attempts == ()


def test_parse_response_handles_missing_model() -> None:
    response = {
        "provider_responses": [{"name": "OpenAI", "http_status": 200}],
    }
    call = parse_response(response)
    assert call.requested_model == ""
    assert call.model_slug == ""
    # Provider string drops the model component when slug is empty
    assert call.provider_string == "openrouter:openai"


# ── provider_chain renderer ──────────────────────────────────────


def test_provider_chain_no_attempts() -> None:
    call = parse_response({"model": "x/y"})
    assert provider_chain(call) == "(no chain reported)"


def test_provider_chain_single() -> None:
    call = parse_response({
        "model": "openai/gpt-4o",
        "provider_responses": [{"name": "OpenAI", "http_status": 200}],
    })
    assert provider_chain(call) == "OpenAI(200)"


def test_provider_chain_fallback() -> None:
    call = parse_response({
        "model": "anthropic/claude-sonnet-4",
        "provider_responses": [
            {"name": "AnthropicVertex", "http_status": 503},
            {"name": "Anthropic", "http_status": 200},
        ],
    })
    assert provider_chain(call) == "AnthropicVertex(503) → Anthropic(200)"


# ── malformed input tolerance ────────────────────────────────────


def test_parse_response_skips_malformed_attempt_entries() -> None:
    """Entries missing `name` or with non-int `http_status` are
    silently dropped — don't crash the adapter on a partial parse."""
    response = {
        "model": "x/y",
        "provider_responses": [
            {"http_status": 200},                          # missing name
            {"name": "", "http_status": 200},              # empty name
            {"name": "Valid", "http_status": "abc"},       # bad status
            {"name": "Real", "http_status": 200},          # ok
        ],
    }
    call = parse_response(response)
    assert len(call.attempts) == 1
    assert call.attempts[0].name == "Real"


def test_parse_response_non_sequence_chain() -> None:
    """If provider_responses is the wrong type (e.g. a string by
    accident), we drop it and proceed without."""
    response = {
        "model": "openai/gpt-4o",
        "provider_responses": "not a list",
    }
    call = parse_response(response)
    assert call.attempts == ()
    # Falls back to slug vendor
    assert call.provider_string == "openrouter:openai-gpt-4o"


def test_canonical_provider_does_not_raise_on_empty_input() -> None:
    """Defensive contract: this helper must never raise. A pure
    transformer that crashes is worse than one that returns a
    well-formed sentinel value."""
    assert canonical_provider(None) == "openrouter:unknown"  # type: ignore[arg-type]
    assert canonical_provider({}) == "openrouter:unknown"


# ── ProviderAttempt invariants ───────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, True),
        (201, True),
        (299, True),
        (300, False),
        (404, False),
        (500, False),
        (503, False),
        (0, False),
    ],
)
def test_provider_attempt_is_success(status: int, expected: bool) -> None:
    assert ProviderAttempt(name="X", http_status=status).is_success is expected


# ── Format invariants ────────────────────────────────────────────


def test_provider_string_always_has_openrouter_prefix() -> None:
    """Every output starts with 'openrouter:' so users can filter
    'all OpenRouter routes' with a single string match."""
    samples: list[dict[str, Any]] = [
        {},
        {"model": "anthropic/claude-sonnet-4"},
        {"model": "x", "provider_responses": [
            {"name": "Y", "http_status": 200},
        ]},
        {"provider_responses": [{"name": "X", "http_status": 500}]},
    ]
    for resp in samples:
        s = canonical_provider(resp)
        assert s.startswith("openrouter:"), (
            f"missing prefix for {resp!r}: {s!r}"
        )


def test_provider_string_is_lowercase_after_prefix() -> None:
    response = {
        "model": "ANTHROPIC/Claude-Sonnet-4",
        "provider_responses": [
            {"name": "AnthropicVertex", "http_status": 200},
        ],
    }
    s = canonical_provider(response)
    # Everything after "openrouter:" should be lowercase
    after = s[len("openrouter:"):]
    assert after == after.lower(), f"non-lowercase tail in {s!r}"


def test_provider_string_is_url_safe() -> None:
    """No spaces, no slashes, no other reserved URL chars in the
    output — Aegis's audit serialization treats provider as a
    grouping key that should be safe to use as a CLI argument."""
    response = {
        "model": "namespace/model-with-dashes",
        "provider_responses": [
            {"name": "Some Vendor With Spaces", "http_status": 200},
        ],
    }
    s = canonical_provider(response)
    assert " " not in s
    # Allowed: lowercase, digits, "-", ":". Anything else is suspect.
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-:")
    assert set(s) <= allowed, f"unexpected chars in {s!r}"
