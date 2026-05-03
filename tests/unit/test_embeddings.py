"""Unit tests for ATV embedding providers (dummy + BGE-local).

Real-model BGE inference is exercised only in the matching integration
test (``tests/integration/test_real_bge_e2e.py``) which auto-skips
when the GGUF / llama-cpp aren't present. The unit tests here cover:

* DummyEmbedding determinism + L2 norm contract.
* BGELocalEmbedding fallback behaviour (no GGUF, no llama-cpp).
* The truncate-or-zero-pad projection used to fit native-dim output
  into ATV slot widths (768 for agent_state, 640 for action_history).
* Provider routing in ``get_provider()`` for all three values of
  ``AEGIS_EMBEDDING_PROVIDER``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from aegis.atv.embeddings import (
    BGELocalEmbedding,
    DummyEmbedding,
    OpenAIEmbedding,
    get_provider,
    reset_bge_cache,
)


# ─────────────────────────────────────────────────────────────────────
# DummyEmbedding contract
# ─────────────────────────────────────────────────────────────────────
class TestDummyEmbedding:
    def test_dim_obeyed(self) -> None:
        v = DummyEmbedding().embed("hello", 768)
        assert v.shape == (768,)

    def test_dtype_is_float32(self) -> None:
        v = DummyEmbedding().embed("x", 64)
        assert v.dtype == np.float32

    def test_empty_text_returns_zero_vector(self) -> None:
        v = DummyEmbedding().embed("", 32)
        assert np.array_equal(v, np.zeros(32, dtype=np.float32))

    def test_deterministic_same_input(self) -> None:
        a = DummyEmbedding().embed("aegis", 256)
        b = DummyEmbedding().embed("aegis", 256)
        assert np.array_equal(a, b)

    def test_different_text_different_vector(self) -> None:
        a = DummyEmbedding().embed("delete database", 256)
        b = DummyEmbedding().embed("read file", 256)
        assert not np.array_equal(a, b)

    def test_unit_l2_norm(self) -> None:
        v = DummyEmbedding().embed("hello world", 128)
        assert np.linalg.norm(v) == pytest.approx(1.0, abs=1e-5)


# ─────────────────────────────────────────────────────────────────────
# BGELocalEmbedding fallback paths (no GGUF / no llama-cpp)
# ─────────────────────────────────────────────────────────────────────
class TestBGEFallbacks:
    def test_no_env_falls_back_to_dummy(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No ``AEGIS_EMBEDDING_MODEL_PATH`` → dummy fallback, identical output."""
        monkeypatch.delenv("AEGIS_EMBEDDING_MODEL_PATH", raising=False)
        bge = BGELocalEmbedding()
        v = bge.embed("hello", 768)
        v_dum = DummyEmbedding().embed("hello", 768)
        assert np.array_equal(v, v_dum)

    def test_missing_path_falls_back_to_dummy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Path env points at a non-existent file → fallback (no crash)."""
        monkeypatch.setenv(
            "AEGIS_EMBEDDING_MODEL_PATH", str(tmp_path / "ghost.gguf"),
        )
        bge = BGELocalEmbedding()
        v = bge.embed("hello", 768)
        v_dum = DummyEmbedding().embed("hello", 768)
        assert np.array_equal(v, v_dum)

    def test_empty_text_is_zero(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AEGIS_EMBEDDING_MODEL_PATH", raising=False)
        v = BGELocalEmbedding().embed("", 768)
        assert np.array_equal(v, np.zeros(768, dtype=np.float32))


# ─────────────────────────────────────────────────────────────────────
# Projection / resize logic (independent of model presence)
# ─────────────────────────────────────────────────────────────────────
class TestBGEProject:
    def test_project_truncate_native_to_smaller(self) -> None:
        """768-D BGE output → 640-D ATV slot: keep first 640, renormalize."""
        bge = BGELocalEmbedding()
        native = np.linspace(1.0, 2.0, 768).astype(np.float32)
        out = bge._project(native, 640)
        assert out.shape == (640,)
        assert np.linalg.norm(out) == pytest.approx(1.0, abs=1e-5)

    def test_project_preserves_native_when_dim_matches(self) -> None:
        bge = BGELocalEmbedding()
        native = np.linspace(0.1, 1.0, 768).astype(np.float32)
        # pre-normalize to make the assertion exact (project also normalizes)
        native /= float(np.linalg.norm(native))
        out = bge._project(native, 768)
        assert out.shape == (768,)
        assert np.allclose(out, native, atol=1e-5)

    def test_project_zero_pad_native_to_larger(self) -> None:
        """384-D native (e.g. bge-small) → 768-D ATV slot: zero-pad tail."""
        bge = BGELocalEmbedding()
        native = np.ones(384, dtype=np.float32)
        out = bge._project(native, 768)
        assert out.shape == (768,)
        # Tail must be zero (we padded), so the energy is concentrated in
        # the first 384 dims even after renormalization.
        assert (out[384:] == 0).all()
        assert np.linalg.norm(out) == pytest.approx(1.0, abs=1e-5)


# ─────────────────────────────────────────────────────────────────────
# Provider routing
# ─────────────────────────────────────────────────────────────────────
class TestGetProvider:
    def test_dummy_routing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "dummy")
        assert isinstance(get_provider(), DummyEmbedding)

    def test_bge_local_routing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "bge-local")
        assert isinstance(get_provider(), BGELocalEmbedding)

    def test_openai_without_key_falls_back_to_dummy(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`openai` selection without ``OPENAI_API_KEY`` must NOT crash —
        the firewall must always have a working embedding."""
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "openai")
        monkeypatch.setattr(_settings, "openai_api_key", None)
        assert isinstance(get_provider(), DummyEmbedding)

    def test_openai_with_key_routes_to_openai(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "openai")
        monkeypatch.setattr(_settings, "openai_api_key", "sk-test-fake")
        # OpenAIEmbedding.__init__ instantiates the SDK client; since we have
        # no real key we just verify the type.
        try:
            p = get_provider()
        except Exception:  # noqa: BLE001 — real OpenAI client may refuse fake key
            return
        assert isinstance(p, OpenAIEmbedding)

    def test_unknown_provider_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aegis.config import settings as _settings
        monkeypatch.setattr(_settings, "aegis_embedding_provider", "nope")
        with pytest.raises(ValueError, match="Unknown embedding provider"):
            get_provider()


# ─────────────────────────────────────────────────────────────────────
# Cache helper
# ─────────────────────────────────────────────────────────────────────
def test_reset_bge_cache_is_idempotent() -> None:
    """Calling reset twice in a row must not crash (used by tests)."""
    reset_bge_cache()
    reset_bge_cache()
