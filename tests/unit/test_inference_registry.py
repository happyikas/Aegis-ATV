"""Unit tests for src/aegis/inference/registry.py.

Covers TOML parsing, schema validation, and the env-var override
on the registry path. Multi-endpoint orchestration tests live in
``test_multi_scrape.py``; this file is parser-only.
"""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from aegis.inference.registry import (
    DEFAULT_TIMEOUT_S,
    EndpointConfig,
    InferenceRegistry,
    InferenceRegistryError,
    default_registry_path,
    load_registry,
)


# ── default path ──────────────────────────────────────────────────


def test_default_path_uses_home() -> None:
    """Without env override, default is ~/.aegis/inference.toml."""
    os.environ.pop("AEGIS_INFERENCE_REGISTRY", None)
    p = default_registry_path()
    assert p.name == "inference.toml"
    assert p.parent.name == ".aegis"


def test_env_override_wins(tmp_path: Path) -> None:
    """AEGIS_INFERENCE_REGISTRY env var overrides default."""
    custom = tmp_path / "custom.toml"
    os.environ["AEGIS_INFERENCE_REGISTRY"] = str(custom)
    try:
        assert default_registry_path() == custom
    finally:
        os.environ.pop("AEGIS_INFERENCE_REGISTRY", None)


# ── empty / missing file ──────────────────────────────────────────


def test_missing_file_returns_empty_registry(tmp_path: Path) -> None:
    """A missing config is not an error — caller falls back to legacy."""
    target = tmp_path / "nonexistent.toml"
    reg = load_registry(target)
    assert reg.is_empty()
    assert reg.endpoints == ()
    assert reg.source_path == target


def test_empty_file_returns_empty_registry(tmp_path: Path) -> None:
    """File exists but has no [endpoints.*] tables."""
    target = tmp_path / "empty.toml"
    target.write_text("")
    reg = load_registry(target)
    assert reg.is_empty()


def test_only_defaults_no_endpoints(tmp_path: Path) -> None:
    """[defaults] alone is valid; just no endpoints."""
    target = tmp_path / "defaults_only.toml"
    target.write_text("[defaults]\ntimeout_s = 5.0\n")
    reg = load_registry(target)
    assert reg.is_empty()
    assert reg.defaults_timeout_s == 5.0


# ── happy-path parsing ────────────────────────────────────────────


def test_single_vllm_endpoint(tmp_path: Path) -> None:
    target = tmp_path / "single.toml"
    target.write_text(dedent("""
        [endpoints.agent-a]
        provider = "vllm"
        metrics_url = "http://10.0.0.10:8000/metrics"
    """).strip())
    reg = load_registry(target)
    assert len(reg.endpoints) == 1
    ep = reg.endpoints[0]
    assert ep.aid == "agent-a"
    assert ep.provider == "vllm"
    assert ep.metrics_url == "http://10.0.0.10:8000/metrics"
    assert ep.timeout_s == DEFAULT_TIMEOUT_S
    assert ep.enabled is True
    assert ep.is_scrapeable() is True


def test_multi_endpoint_mixed_providers(tmp_path: Path) -> None:
    target = tmp_path / "multi.toml"
    target.write_text(dedent("""
        [defaults]
        timeout_s = 3.0

        [endpoints.agent-b]
        provider = "vllm"
        metrics_url = "http://10.0.0.20:8000/metrics"
        timeout_s = 5.0

        [endpoints.agent-a]
        provider = "vllm"
        metrics_url = "http://10.0.0.10:8000/metrics"

        [endpoints.agent-c]
        provider = "cloud"
        provider_name = "anthropic-claude-3-5"
    """).strip())
    reg = load_registry(target)
    assert len(reg.endpoints) == 3
    # Endpoints are sorted by aid for stability.
    aids = [ep.aid for ep in reg.endpoints]
    assert aids == ["agent-a", "agent-b", "agent-c"]
    # Defaults applied where not overridden.
    assert reg.endpoints[0].timeout_s == 3.0   # default
    assert reg.endpoints[1].timeout_s == 5.0   # explicit override
    # Cloud provider is recorded but not scrapeable.
    cloud = reg.endpoints[2]
    assert cloud.provider == "cloud"
    assert cloud.provider_name == "anthropic-claude-3-5"
    assert cloud.is_scrapeable() is False


def test_disabled_endpoint(tmp_path: Path) -> None:
    target = tmp_path / "disabled.toml"
    target.write_text(dedent("""
        [endpoints.agent-paused]
        provider = "vllm"
        metrics_url = "http://10.0.0.30:8000/metrics"
        enabled = false
    """).strip())
    reg = load_registry(target)
    ep = reg.endpoints[0]
    assert ep.enabled is False
    assert ep.is_scrapeable() is False


def test_by_aid_lookup(tmp_path: Path) -> None:
    target = tmp_path / "lookup.toml"
    target.write_text(dedent("""
        [endpoints.alpha]
        provider = "vllm"
        metrics_url = "http://localhost:8000/metrics"

        [endpoints.beta]
        provider = "cloud"
    """).strip())
    reg = load_registry(target)
    assert reg.by_aid("alpha") is not None
    assert reg.by_aid("alpha").provider == "vllm"
    assert reg.by_aid("beta").provider == "cloud"
    assert reg.by_aid("nonexistent") is None


# ── validation errors ────────────────────────────────────────────


def test_malformed_toml(tmp_path: Path) -> None:
    target = tmp_path / "bad.toml"
    target.write_text("not = valid\nbroken syntax [")
    with pytest.raises(InferenceRegistryError, match="malformed TOML"):
        load_registry(target)


def test_unknown_provider_rejected(tmp_path: Path) -> None:
    target = tmp_path / "unknown.toml"
    target.write_text(dedent("""
        [endpoints.x]
        provider = "snake-oil"
    """).strip())
    with pytest.raises(InferenceRegistryError, match="provider must be one of"):
        load_registry(target)


def test_vllm_without_metrics_url_rejected(tmp_path: Path) -> None:
    target = tmp_path / "no-url.toml"
    target.write_text(dedent("""
        [endpoints.x]
        provider = "vllm"
    """).strip())
    with pytest.raises(InferenceRegistryError, match="requires metrics_url"):
        load_registry(target)


def test_negative_timeout_rejected(tmp_path: Path) -> None:
    target = tmp_path / "neg.toml"
    target.write_text(dedent("""
        [endpoints.x]
        provider = "vllm"
        metrics_url = "http://x:8000/metrics"
        timeout_s = -1
    """).strip())
    with pytest.raises(InferenceRegistryError, match="timeout_s must be > 0"):
        load_registry(target)


def test_zero_default_timeout_rejected(tmp_path: Path) -> None:
    target = tmp_path / "zerodef.toml"
    target.write_text(dedent("""
        [defaults]
        timeout_s = 0
    """).strip())
    with pytest.raises(InferenceRegistryError, match="defaults.timeout_s"):
        load_registry(target)


def test_metrics_url_wrong_type_rejected(tmp_path: Path) -> None:
    target = tmp_path / "wrong-url.toml"
    target.write_text(dedent("""
        [endpoints.x]
        provider = "vllm"
        metrics_url = 42
    """).strip())
    with pytest.raises(InferenceRegistryError, match="metrics_url must be a string"):
        load_registry(target)


def test_provider_name_wrong_type_rejected(tmp_path: Path) -> None:
    target = tmp_path / "wrong-name.toml"
    target.write_text(dedent("""
        [endpoints.x]
        provider = "cloud"
        provider_name = 42
    """).strip())
    with pytest.raises(InferenceRegistryError, match="provider_name must be a string"):
        load_registry(target)


def test_endpoints_not_a_table(tmp_path: Path) -> None:
    target = tmp_path / "wrong-shape.toml"
    target.write_text("endpoints = \"oops\"")
    with pytest.raises(InferenceRegistryError, match=r"\[endpoints\]"):
        load_registry(target)


# ── EndpointConfig.is_scrapeable matrix ──────────────────────────


def test_is_scrapeable_matrix() -> None:
    # vllm + url + enabled → scrapeable
    assert EndpointConfig(
        aid="a", provider="vllm",
        metrics_url="http://x/metrics", enabled=True,
    ).is_scrapeable()
    # vllm + url + disabled → not
    assert not EndpointConfig(
        aid="a", provider="vllm",
        metrics_url="http://x/metrics", enabled=False,
    ).is_scrapeable()
    # cloud → never
    assert not EndpointConfig(aid="a", provider="cloud").is_scrapeable()
    # vllm without url → not (defensive; load_registry rejects this)
    assert not EndpointConfig(
        aid="a", provider="vllm", metrics_url=None,
    ).is_scrapeable()


# ── InferenceRegistry helpers ────────────────────────────────────


def test_empty_registry_is_empty() -> None:
    assert InferenceRegistry().is_empty()
    assert not InferenceRegistry(
        endpoints=(EndpointConfig(aid="a", provider="cloud"),),
    ).is_empty()
