"""Embedding provider abstraction.

Three backends are wired:

* ``openai`` — calls ``text-embedding-3-small`` with the requested ``dim``.
* ``dummy`` — deterministic SHA3-based pseudo-embeddings; no API key needed.
  This is the default in dev/test so the service runs without OPENAI_API_KEY.
* ``bge-local`` — placeholder for sentence-transformers / BGE; not implemented
  in MVP first cut.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

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


class BGELocalEmbedding(EmbeddingProvider):
    def embed(self, text: str, dim: int) -> np.ndarray:
        raise NotImplementedError("BGE local embedding is a stretch goal (PLAN 6.2)")


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
