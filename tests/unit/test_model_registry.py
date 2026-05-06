"""Unit tests for the Solo Free local-sLLM model registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from aegis.judge.model_registry import (
    DEFAULT_MODEL_NAME,
    ModelSpec,
    default_model,
    get_model,
    list_models,
    model_target_path,
)


def test_default_model_is_llama_1b() -> None:
    """Solo Free default — small enough to download, big enough to reason."""
    assert DEFAULT_MODEL_NAME == "llama-3.2-1b"
    spec = default_model()
    assert spec.name == "llama-3.2-1b"
    assert spec.size_mb < 1000  # under 1 GB → reasonable first-install


def test_list_models_returns_at_least_three() -> None:
    """Catalogue must offer small / default / strong tiers."""
    models = list_models()
    assert len(models) >= 3
    names = [m.name for m in models]
    # The default must appear in the list.
    assert DEFAULT_MODEL_NAME in names


def test_list_models_each_has_required_fields() -> None:
    for m in list_models():
        assert m.name
        assert m.url.startswith("https://")
        assert m.size_mb > 0
        assert m.description
        assert m.license


def test_get_model_lookup_by_name() -> None:
    spec = get_model("qwen-0.5b")
    assert spec.name == "qwen-0.5b"
    assert spec.size_mb < spec.size_mb + 1  # tautology — exercises field access


def test_get_model_unknown_raises_keyerror_with_help() -> None:
    with pytest.raises(KeyError) as exc:
        get_model("gpt-9000")
    msg = str(exc.value)
    assert "gpt-9000" in msg
    assert "Known:" in msg
    # All registered names appear in the error so users know their options.
    for m in list_models():
        assert m.name in msg


def test_model_target_path_uses_filename(tmp_path: Path) -> None:
    spec = default_model()
    target = model_target_path(spec, tmp_path)
    assert target.parent == tmp_path
    assert target.name.endswith(".gguf")


def test_model_spec_local_filename_falls_back_to_url_basename() -> None:
    """When ``filename`` is unset, derive from the URL path."""
    spec = ModelSpec(
        name="t", description="t", url="https://example.com/path/foo.gguf",
        size_mb=1,
    )
    assert spec.local_filename() == "foo.gguf"


def test_model_spec_explicit_filename_wins() -> None:
    spec = ModelSpec(
        name="t", description="t", url="https://example.com/foo.gguf",
        size_mb=1, filename="renamed.gguf",
    )
    assert spec.local_filename() == "renamed.gguf"


def test_all_default_urls_are_huggingface() -> None:
    """We ship HF URLs only — privacy-friendly, no telemetry, no auth needed."""
    for m in list_models():
        assert "huggingface.co" in m.url, (
            f"non-HF source {m.url} for model {m.name} — Solo Free should "
            "use HF only so users know what's downloading"
        )


def test_default_model_url_targets_q4_gguf() -> None:
    """The default GGUF must be Q4_K_M quantization — smallest CPU-viable."""
    url = default_model().url.lower()
    assert "q4" in url


# ── PR 5: aliases + recommendations ───────────────────────────────────


def test_phi35_mini_has_phi3_aliases() -> None:
    """phi3-mini and phi-3-mini are short aliases for phi-3.5-mini —
    matches the slug users may try when copy-pasting from Phi-3 docs."""
    spec = get_model("phi-3.5-mini")
    assert "phi3-mini" in spec.aliases
    assert "phi-3-mini" in spec.aliases


def test_get_model_resolves_alias() -> None:
    canonical = get_model("phi-3.5-mini")
    assert get_model("phi3-mini") is canonical
    assert get_model("phi-3-mini") is canonical


def test_get_model_unknown_alias_raises_with_hint() -> None:
    with pytest.raises(KeyError, match="alias"):
        get_model("does-not-exist")


def test_list_aliases_round_trips() -> None:
    from aegis.judge.model_registry import list_aliases
    aliases = list_aliases()
    # Every model's canonical name maps to itself.
    for m in list_models():
        assert aliases[m.name] == m.name
    # Every alias maps to a canonical name.
    for m in list_models():
        for a in m.aliases:
            assert aliases[a] == m.name


def test_recommendations_include_use_case_for_each_judge() -> None:
    from aegis.judge.model_registry import render_recommendations
    out = render_recommendations()
    assert "llama-3.2-1b" in out
    assert "phi-3.5-mini" in out
    assert "RAG is ON" in out  # phi recommendation
    assert "RAG is OFF" in out  # llama recommendation
    # No embedding models in recommendations (judge-only output).
    assert "bge-base-en" not in out


def test_phi_recommended_for_rag() -> None:
    """The RAG-grounded judge stack must point at phi-3.5-mini."""
    spec = get_model("phi-3.5-mini")
    assert "RAG is ON" in spec.recommended_for
    assert "★" in spec.recommended_for


def test_recommended_for_present_for_all_judges() -> None:
    """Every judge must carry a use-case hint so --recommend produces
    useful output for the entire registry."""
    from aegis.judge.model_registry import list_models_by_kind
    for m in list_models_by_kind("judge"):
        assert m.recommended_for, (
            f"judge {m.name!r} missing recommended_for"
        )
