"""End-to-end test: real BGE-base-en encoder produces semantic embeddings.

Skips automatically when:

* ``llama-cpp-python`` is not installed (CI / containers without it).
* No ``bge-*.gguf`` is present in ``./models/`` (fresh checkout
  pre-``aegis pull-model --model bge-base-en``).

When both are present (Mac mini after the pull-model step), this test
runs actual BGE inference and verifies the contract that the ATV
builder relies on:

1. The judge enters real-mode (not dummy fallback).
2. Output shape matches the requested ``dim`` (768 / 640).
3. Output is L2-normalized (cosine geometry preserved).
4. **Semantic clustering** — semantically similar texts produce
   higher cosine similarity than unrelated texts. This is the test
   that proves "BGE actually understands meaning"; without it we
   could be silently encoding noise.
5. Determinism — same text twice → bit-identical vector (greedy
   inference, required for ``aegis verify-audit`` replay).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import numpy as np
import pytest

from aegis.atv.embeddings import (
    BGELocalEmbedding,
    DummyEmbedding,
    reset_bge_cache,
)


def _llama_cpp_installed() -> bool:
    return importlib.util.find_spec("llama_cpp") is not None


def _find_bge_gguf() -> Path | None:
    """Find any ``bge-*.gguf`` in the repo's ``models/`` directory."""
    repo_root = Path(__file__).resolve().parents[2]
    models_dir = repo_root / "models"
    if not models_dir.exists():
        return None
    for p in sorted(models_dir.glob("bge-*.gguf")):
        if p.is_file() and p.stat().st_size > 10_000_000:  # >10 MB → real
            return p
    return None


pytestmark = [
    pytest.mark.skipif(
        not _llama_cpp_installed(),
        reason="llama-cpp-python not installed (uv sync --extra local-llm)",
    ),
    pytest.mark.skipif(
        _find_bge_gguf() is None,
        reason="no BGE GGUF in models/ (run: aegis pull-model --model bge-base-en)",
    ),
]


@pytest.fixture
def real_bge(monkeypatch: pytest.MonkeyPatch) -> BGELocalEmbedding:
    gguf = _find_bge_gguf()
    assert gguf is not None
    monkeypatch.setenv("AEGIS_EMBEDDING_MODEL_PATH", str(gguf))
    reset_bge_cache()
    return BGELocalEmbedding()


def test_bge_real_mode_active(real_bge: BGELocalEmbedding) -> None:
    """Canary: the judge must NOT be in dummy fallback mode.

    We assert this by computing two embeddings whose dummy SHA3 outputs
    are deterministic; if real BGE is active the cosine of two similar
    phrases will diverge from the dummy baseline.
    """
    v_real = real_bge.embed("destroy production database", 768)
    v_dummy = DummyEmbedding().embed("destroy production database", 768)
    # In real mode the model output diverges from the SHA3 noise.
    assert not np.allclose(v_real, v_dummy, atol=1e-3), (
        "BGE silently fell back to dummy — model file or llama-cpp may be broken"
    )


def test_bge_output_is_unit_norm(real_bge: BGELocalEmbedding) -> None:
    v = real_bge.embed("hello world", 768)
    assert v.shape == (768,)
    assert np.linalg.norm(v) == pytest.approx(1.0, abs=1e-3)


def test_bge_output_dim_is_obeyed(real_bge: BGELocalEmbedding) -> None:
    """ATV asks for various dims (768 for agent_state, 640 for action_history).
    Both must come back the right size."""
    v_768 = real_bge.embed("test", 768)
    v_640 = real_bge.embed("test", 640)
    assert v_768.shape == (768,)
    assert v_640.shape == (640,)


def test_bge_semantic_clustering(real_bge: BGELocalEmbedding) -> None:
    """The whole point of switching from dummy to BGE: similar texts must
    cluster, dissimilar texts must not.

    Threshold of 0.10 separation is conservative — real BGE typically
    shows > 0.30 separation on this kind of pair. If this fails, the
    encoder is malfunctioning at the semantic level.
    """
    v_destructive_a = real_bge.embed("delete production database", 768)
    v_destructive_b = real_bge.embed("drop production tables", 768)
    v_benign = real_bge.embed("list files in temp directory", 768)

    sim_pos = float(np.dot(v_destructive_a, v_destructive_b))
    sim_neg = float(np.dot(v_destructive_a, v_benign))
    assert sim_pos > sim_neg + 0.10, (
        f"BGE has no semantic signal: cos(destructive, destructive)={sim_pos:.3f} "
        f"vs cos(destructive, benign)={sim_neg:.3f}"
    )


def test_bge_deterministic_same_input(real_bge: BGELocalEmbedding) -> None:
    """Greedy inference → bit-identical vectors for the same input.
    Required for audit-replay over an embedding-derived ATV slot."""
    v1 = real_bge.embed("the quick brown fox", 768)
    v2 = real_bge.embed("the quick brown fox", 768)
    assert np.array_equal(v1, v2)


def test_bge_via_atv_builder_produces_real_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: when ``AEGIS_EMBEDDING_PROVIDER=bge-local``, the
    ATV builder's ``encode_agent_state_embedding`` must emit a vector
    that's NOT the dummy SHA3 baseline."""
    import time

    gguf = _find_bge_gguf()
    assert gguf is not None
    monkeypatch.setenv("AEGIS_EMBEDDING_MODEL_PATH", str(gguf))
    monkeypatch.setenv("AEGIS_EMBEDDING_PROVIDER", "bge-local")

    from aegis.config import settings as _settings
    monkeypatch.setattr(_settings, "aegis_embedding_provider", "bge-local")

    # Reset caches (provider singleton + bge llm).
    reset_bge_cache()

    from aegis.atv.builder import encode_agent_state_embedding
    from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics

    inp = ATVInput(
        header=ATVHeader(
            trace_id="t", span_id="s", tenant_id="t", aid="a",
            timestamp_ns=time.time_ns(),
        ),
        agent_state_text="user wants to delete production database",
        plan_text="DROP TABLE",
        tool_name="Bash",
        tool_args_json='{"command":"psql"}',
        safety_flags={},
        memory_fingerprint="sha3:t",
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=1, output_token_count=1,
        ),
    )

    # Force dummy as a baseline reference.
    monkeypatch.setattr(_settings, "aegis_embedding_provider", "dummy")
    v_dum = encode_agent_state_embedding(inp)

    # Now switch to bge-local and re-encode.
    monkeypatch.setattr(_settings, "aegis_embedding_provider", "bge-local")
    reset_bge_cache()
    v_real = encode_agent_state_embedding(inp)

    # The two must differ (real signal vs SHA3 noise).
    assert not np.allclose(v_real, v_dum, atol=1e-3), (
        "ATV builder did not actually swap to BGE provider"
    )
    # Both must be 768-D.
    assert v_real.shape == (768,)
    assert v_dum.shape == (768,)
    # Real BGE output must be unit-norm or near-zero (zero-input case).
    n = float(np.linalg.norm(v_real))
    assert n == pytest.approx(1.0, abs=1e-3), f"non-unit norm: {n}"
    # Confirm we didn't accidentally pollute env for downstream tests.
    os.environ.pop("AEGIS_EMBEDDING_MODEL_PATH", None)
    os.environ.pop("AEGIS_EMBEDDING_PROVIDER", None)
