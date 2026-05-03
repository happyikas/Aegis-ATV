"""Embedding provider abstraction.

Three backends are wired:

* ``openai`` — calls ``text-embedding-3-small`` with the requested ``dim``.
  Requires ``OPENAI_API_KEY``. Hard-disabled when the key is absent.
* ``dummy`` — deterministic SHA3-based pseudo-embeddings; no API key needed.
  Same text → same vector, but no semantic meaning. Used in dev/test and
  as the fallback when neither real provider is configured.
* ``bge-local`` — **Solo Free real embedding.** BGE-base-en-v1.5 GGUF
  loaded via ``llama-cpp-python`` in ``embedding=True`` mode. 768-D
  native output, ~5–10 ms / text on M1 CPU. Requires the optional
  ``aegis-mvp[local-llm]`` extra and a downloaded GGUF
  (``aegis pull-model --model bge-base-en``). Falls back to
  :class:`DummyEmbedding` automatically if the model file or the
  ``llama_cpp`` wheel is missing — never blocks the firewall.

Provider selection lives in :func:`get_provider`, driven by the
``AEGIS_EMBEDDING_PROVIDER`` env var (one of ``openai`` / ``dummy`` /
``bge-local``).
"""

from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from aegis.config import settings

if TYPE_CHECKING:
    pass


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, text: str, dim: int) -> np.ndarray: ...


class DummyEmbedding(EmbeddingProvider):
    """Deterministic pseudo-embedding from SHA3-512 expansion.

    Output is L2-normalized so vector arithmetic behaves sensibly. Same text
    always yields the same vector across runs and machines.
    """

    def embed(self, text: str, dim: int) -> np.ndarray:
        if not text:
            return np.zeros(dim, dtype=np.float32)
        out = np.zeros(dim, dtype=np.float32)
        seed = text.encode("utf-8")
        chunk_idx = 0
        i = 0
        while i < dim:
            digest = hashlib.sha3_512(seed + chunk_idx.to_bytes(4, "big")).digest()
            for b in digest:
                if i >= dim:
                    break
                out[i] = (b - 127.5) / 127.5
                i += 1
            chunk_idx += 1
        norm = float(np.linalg.norm(out))
        if norm > 0:
            out /= norm
        return out


class OpenAIEmbedding(EmbeddingProvider):
    def __init__(self) -> None:
        from openai import OpenAI

        self.client = OpenAI()
        self.model = settings.aegis_embedding_model

    def embed(self, text: str, dim: int) -> np.ndarray:
        if not text:
            return np.zeros(dim, dtype=np.float32)
        resp = self.client.embeddings.create(
            model=self.model,
            input=text[:8000],
            dimensions=dim,
        )
        return np.array(resp.data[0].embedding, dtype=np.float32)


@lru_cache(maxsize=2)
def _load_bge_llm(model_path_str: str) -> Any:
    """Load llama-cpp Llama instance in embedding-only mode.

    Cached at process scope so repeated calls during one Claude Code
    session don't re-load the 100 MB GGUF. Returns ``None`` when the
    optional ``llama-cpp-python`` extra is missing or the file fails
    to open — :class:`BGELocalEmbedding` then falls back to dummy.
    """
    try:
        # ``llama_cpp`` is the optional ``aegis-mvp[local-llm]`` extra;
        # CI sees import-not-found, local dev with the extra installed
        # sees unused-ignore. Suppress both.
        from llama_cpp import Llama  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return None
    try:
        return Llama(
            model_path=model_path_str,
            n_ctx=512,        # BGE max context is 512 tokens; no point larger
            n_threads=4,
            embedding=True,   # critical: switches the model to embed mode
            verbose=False,
        )
    except Exception:  # noqa: BLE001 — any load failure → dummy fallback
        return None


class BGELocalEmbedding(EmbeddingProvider):
    """Solo Free real embedding via BGE-base-en-v1.5 (GGUF, llama-cpp).

    Three operating modes (matched on construction, not per-call, so
    the cache keys are stable):

    * **real** — ``AEGIS_EMBEDDING_MODEL_PATH`` points at an existing
      GGUF and ``llama-cpp-python`` is importable. Inference runs the
      real BGE encoder; output is L2-normalized 768-D (or whatever the
      model's native dim is) then projected to the requested ``dim``
      via truncate-or-zero-pad. This preserves the high-energy front
      of the vector while still satisfying the ATV slot length.
    * **dummy fallback** — model file missing OR llama-cpp missing.
      Delegates to :class:`DummyEmbedding` so the firewall never
      blocks. The per-call hot path silently degrades; users learn
      about the degraded mode via ``aegis report -v`` (the audit log
      records the embedding source).

    Determinism: BGE inference is greedy + bit-deterministic on a
    given hardware/library combo, so ``aegis verify-audit`` can
    reproduce a past ATV given the same GGUF.
    """

    def __init__(self) -> None:
        self._fallback = DummyEmbedding()

    def _resolve_model_path(self) -> Path | None:
        raw = os.environ.get("AEGIS_EMBEDDING_MODEL_PATH", "").strip()
        if not raw:
            raw = settings.aegis_embedding_model_path.strip()
        if not raw:
            return None
        p = Path(raw)
        return p if p.exists() else None

    def _project(self, vec: np.ndarray, dim: int) -> np.ndarray:
        """Resize a (native_dim,) embedding to (dim,) via truncate+pad.

        BGE-base outputs 768-D; ATV asks for 768 (agent_state) or 640
        (action_history). For 640 we keep the first 640 dims of the
        L2-normalized vector — high-energy front is information-dense
        in BERT-family encoders. For larger ``dim`` we zero-pad the
        tail. Re-normalize so output is unit L2 in either case.
        """
        cur = int(vec.shape[0])
        if cur == dim:
            out = vec
        elif cur > dim:
            out = vec[:dim]
        else:
            out = np.zeros(dim, dtype=np.float32)
            out[:cur] = vec
        norm = float(np.linalg.norm(out))
        if norm > 0:
            out = out / norm
        return out.astype(np.float32, copy=False)

    def embed(self, text: str, dim: int) -> np.ndarray:
        if not text:
            return np.zeros(dim, dtype=np.float32)
        path = self._resolve_model_path()
        if path is None:
            return self._fallback.embed(text, dim)
        llm = _load_bge_llm(str(path))
        if llm is None:
            return self._fallback.embed(text, dim)
        try:
            # llama-cpp returns either {"data":[{"embedding":[...]}]} or
            # the raw list, depending on version. Handle both.
            raw = llm.create_embedding(text[:8000])
            if isinstance(raw, dict):
                vec = np.asarray(
                    raw["data"][0]["embedding"], dtype=np.float32,
                )
            else:
                vec = np.asarray(raw, dtype=np.float32).ravel()
        except Exception:  # noqa: BLE001 — degrade rather than crash
            return self._fallback.embed(text, dim)
        if vec.size == 0:
            return self._fallback.embed(text, dim)
        # L2-normalize the raw native-dim output before projection so
        # truncation preserves cosine geometry.
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return self._project(vec, dim)


def reset_bge_cache() -> None:
    """Test helper — drop the cached llama-cpp Llama so a re-pointed
    GGUF picks up. Mirrors :func:`aegis.judge.local_phi.reset_model_hash_cache`.
    """
    _load_bge_llm.cache_clear()


def get_provider() -> EmbeddingProvider:
    provider = settings.aegis_embedding_provider
    if provider == "openai":
        if not settings.openai_api_key:
            return DummyEmbedding()
        return OpenAIEmbedding()
    if provider == "dummy":
        return DummyEmbedding()
    if provider == "bge-local":
        return BGELocalEmbedding()
    raise ValueError(f"Unknown embedding provider: {provider}")
