"""Solo Free local-sLLM model registry.

Picks the GGUF a Solo Free user gets when they run ``aegis pull-model``.
The default is **Llama-3.2-1B-Instruct Q4_K_M** — chosen for the
following Solo Free constraints:

* Must run on a Mac mini (Apple Silicon or Intel) with no GPU.
* Must download in under 5 minutes on a typical home connection (so
  the install never feels broken).
* Must reliably emit a one-line JSON verdict on a structured prompt.
* License must permit redistribution.

Llama-3.2-1B-Instruct Q4_K_M:

* ~770 MB GGUF, ~50–100 ms / verdict on M1/M2 CPU-only.
* Llama 3.2 Community License (commercial use allowed under 700M MAU
  threshold — fits Solo Free).
* Bartowski's repackaging on HuggingFace is the canonical Q4_K_M GGUF
  used across the open-source LLM tooling ecosystem.

Smaller alternatives (``qwen-0.5b``) and stronger alternatives
(``phi-4-mini``) are available via ``--model NAME`` for users who want
to optimise for size or quality. The registry table below is the
single source of truth — adding a new model is a one-line entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ModelKind = Literal["judge", "embedding"]


@dataclass(frozen=True)
class ModelSpec:
    """A downloadable GGUF model entry.

    The ``kind`` discriminator splits the registry into two families:

    * ``judge`` — instruction-following LLMs that emit ALLOW/BLOCK/
      REQUIRE_APPROVAL JSON. Fed via ``AEGIS_JUDGE_MODEL_PATH`` to
      :class:`aegis.judge.local_phi.LocalPhiJudge`.
    * ``embedding`` — sentence encoders (BGE, E5, etc.). Fed via
      ``AEGIS_EMBEDDING_MODEL_PATH`` to
      :class:`aegis.atv.embeddings.BGELocalEmbedding`. The
      ``embedding_dim`` field records the model's native output
      dimension; the embedding adapter projects/truncates to whatever
      ATV slot length the encoder requests.
    """

    name: str                # short slug (also CLI arg value)
    description: str         # human-readable
    url: str                 # direct GGUF URL (HF resolve link)
    size_mb: int             # approximate, for progress UX
    kind: ModelKind = "judge"
    embedding_dim: int = 0   # native output dim for kind="embedding"; 0 otherwise
    sha256: str | None = None  # optional integrity check (None = skip)
    license: str = "see model card"
    filename: str = ""       # local filename (defaults to URL basename)

    def local_filename(self) -> str:
        return self.filename or self.url.rsplit("/", 1)[-1]


# ─────────────────────────────────────────────────────────────────────
# Registered models — order matters for --list output
# ─────────────────────────────────────────────────────────────────────
_REGISTRY: list[ModelSpec] = [
    ModelSpec(
        name="llama-3.2-1b",
        description=(
            "Llama 3.2 1B Instruct Q4_K_M — Solo Free default. "
            "770 MB, ~80 ms/verdict on M1 CPU. Reliable JSON output."
        ),
        url=(
            "https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/"
            "resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf"
        ),
        size_mb=770,
        license="Llama 3.2 Community License",
    ),
    ModelSpec(
        name="qwen-0.5b",
        description=(
            "Qwen 2.5 0.5B Instruct Q4_K_M — smallest viable option. "
            "400 MB, ~30 ms/verdict, but JSON adherence less robust."
        ),
        url=(
            "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/"
            "resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf"
        ),
        size_mb=400,
        license="Apache-2.0",
    ),
    ModelSpec(
        name="phi-3.5-mini",
        description=(
            "Phi-3.5 Mini Instruct Q4_K_M — strongest free option. "
            "2.2 GB, ~150 ms/verdict on M1 CPU. Best classification accuracy."
        ),
        url=(
            "https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF/"
            "resolve/main/Phi-3.5-mini-instruct-Q4_K_M.gguf"
        ),
        size_mb=2200,
        license="MIT",
    ),
    # ── Embedding models (Solo Free default for ATV agent_state_embedding) ─
    ModelSpec(
        name="bge-base-en",
        description=(
            "BGE-base-en-v1.5 Q4_K_M — Solo Free embedding default. "
            "100 MB, 768-D native (matches ATV agent_state_embedding slot). "
            "MTEB 63.55 — competitive with OpenAI ada-002. ~5-10 ms/text on M1."
        ),
        url=(
            "https://huggingface.co/CompendiumLabs/bge-base-en-v1.5-gguf/"
            "resolve/main/bge-base-en-v1.5-q4_k_m.gguf"
        ),
        size_mb=100,
        kind="embedding",
        embedding_dim=768,
        license="MIT",
    ),
    ModelSpec(
        name="bge-small-en",
        description=(
            "BGE-small-en-v1.5 Q4_K_M — smaller embedder. "
            "33 MB, 384-D native (ATV adapter projects/pads to 768). "
            "MTEB 62.17 — slightly below base-en but 3× smaller."
        ),
        url=(
            "https://huggingface.co/CompendiumLabs/bge-small-en-v1.5-gguf/"
            "resolve/main/bge-small-en-v1.5-q4_k_m.gguf"
        ),
        size_mb=33,
        kind="embedding",
        embedding_dim=384,
        license="MIT",
    ),
]

DEFAULT_MODEL_NAME = "llama-3.2-1b"
DEFAULT_EMBEDDING_NAME = "bge-base-en"


def list_models() -> list[ModelSpec]:
    """Return the catalogue (ordered)."""
    return list(_REGISTRY)


def get_model(name: str) -> ModelSpec:
    """Look up by short name. Raises ``KeyError`` on miss."""
    for m in _REGISTRY:
        if m.name == name:
            return m
    raise KeyError(
        f"unknown model {name!r}. Known: " + ", ".join(m.name for m in _REGISTRY)
    )


def default_model() -> ModelSpec:
    """Default *judge* GGUF (back-compat alias)."""
    return get_model(DEFAULT_MODEL_NAME)


def default_embedding_model() -> ModelSpec:
    """Default *embedding* GGUF for Solo Free (BGE-base-en)."""
    return get_model(DEFAULT_EMBEDDING_NAME)


def model_target_path(spec: ModelSpec, models_dir: Path) -> Path:
    """Where the GGUF lives after pull-model: ``<models_dir>/<filename>``."""
    return models_dir / spec.local_filename()


def list_models_by_kind(kind: ModelKind) -> list[ModelSpec]:
    """All registered models of one kind, in registry order."""
    return [m for m in _REGISTRY if m.kind == kind]


__all__ = [
    "DEFAULT_EMBEDDING_NAME",
    "DEFAULT_MODEL_NAME",
    "ModelKind",
    "ModelSpec",
    "default_embedding_model",
    "default_model",
    "get_model",
    "list_models",
    "list_models_by_kind",
    "model_target_path",
]
