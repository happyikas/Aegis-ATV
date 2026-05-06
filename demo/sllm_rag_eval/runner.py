"""Drive each :class:`BenchmarkCase` through configured judge + RAG.

A single :class:`Configuration` describes one combination of
*judge provider* (DummyJudge / LocalPhiJudge / HaikuJudge) × *RAG
toggle* (True / False). The runner reports per-case predicted decision
+ retrieved chunk IDs + latency.
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cases import BenchmarkCase

_REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Configuration:
    name: str                           # short label, e.g. "sllm-rag"
    judge: str                          # "dummy" / "local-phi" / "haiku"
    rag_enabled: bool
    description: str = ""

    @property
    def slug(self) -> str:
        rag = "rag" if self.rag_enabled else "norag"
        return f"{self.judge}-{rag}"


@dataclass
class CaseResult:
    cid: str
    config_slug: str
    expected: str
    predicted: str
    correct: bool
    duration_ms: float
    retrieved_chunk_ids: list[str] = field(default_factory=list)
    retrieval_recall: float = 0.0
    raw_reason: str = ""
    error: str = ""


@dataclass
class ConfigurationReport:
    config: Configuration
    skipped: bool
    skip_reason: str = ""
    results: list[CaseResult] = field(default_factory=list)
    total_ms: float = 0.0

    @property
    def n_correct(self) -> int:
        return sum(1 for r in self.results if r.correct)

    @property
    def n_total(self) -> int:
        return len(self.results)

    @property
    def accuracy(self) -> float:
        return self.n_correct / self.n_total if self.n_total else 0.0

    @property
    def mean_recall(self) -> float:
        scored = [r for r in self.results if r.retrieval_recall > 0 or r.retrieved_chunk_ids]
        if not scored:
            return 0.0
        return sum(r.retrieval_recall for r in scored) / len(scored)


def _set_provider(config: Configuration) -> None:
    """Mutate process env so the next judge import sees this config."""
    os.environ["AEGIS_JUDGE_PROVIDER"] = config.judge
    os.environ["AEGIS_RAG_ENABLED"] = "1" if config.rag_enabled else "0"


def _retrieve_chunks(query: str, k: int = 3) -> list[str]:
    try:
        from aegis.atv.embeddings import DummyEmbedding
        from aegis.judge.rag_corpus import reset_corpus_cache
        from aegis.judge.rag_retrieval import (
            build_default_index,
            reset_index_cache,
            retrieve,
        )
        reset_corpus_cache()
        reset_index_cache()
        index = build_default_index()
        hits = retrieve(query, k=k, index=index, provider=DummyEmbedding())
        return [c.id for c, _ in hits]
    except Exception:  # noqa: BLE001 — eval only
        return []


def _build_judge(config: Configuration) -> Any:
    """Return a Judge instance per the named provider. Returns None
    if the provider isn't usable in this environment (caller skips
    the configuration)."""
    if config.judge == "dummy":
        from aegis.judge.dummy import DummyJudge
        return DummyJudge()
    if config.judge == "local-phi":
        path = os.environ.get("AEGIS_JUDGE_MODEL_PATH", "")
        if not path or not Path(path).is_file():
            return None
        from aegis.judge.local_phi import LocalPhiJudge
        return LocalPhiJudge()
    if config.judge == "haiku":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        from aegis.judge.haiku import HaikuJudge
        try:
            return HaikuJudge()
        except Exception:  # noqa: BLE001
            return None
    raise ValueError(f"unknown judge: {config.judge}")


def _build_summary_with_rag(case: BenchmarkCase, retrieved_ids: list[str]) -> str:
    """Compose the judge's input message: case summary + (optional) RAG block.

    Mirrors ``aegis.judge.haiku._build_user_message`` so each judge sees
    the same prompt shape."""
    if not retrieved_ids:
        return case.summary
    from aegis.judge.rag_corpus import RagChunk, load_default_corpus
    corpus = load_default_corpus()
    chunks: list[RagChunk] = []
    for cid in retrieved_ids:
        c = corpus.by_id(cid)
        if c is not None:
            chunks.append(c)
    if not chunks:
        return case.summary
    block = "\n\n".join(c.render_for_prompt() for c in chunks)
    return (
        f"{case.summary}\n\n"
        "## Relevant policy / incident context\n\n"
        f"{block}"
    )


def run_case(judge: Any, config: Configuration, case: BenchmarkCase) -> CaseResult:
    retrieved: list[str] = []
    if config.rag_enabled:
        retrieved = _retrieve_chunks(case.summary, k=3)

    # Compute retrieval recall against ground-truth chunk IDs.
    if case.expected_chunk_ids and retrieved:
        hits = sum(1 for cid in case.expected_chunk_ids if cid in retrieved)
        recall = hits / len(case.expected_chunk_ids)
    else:
        recall = 0.0

    summary = _build_summary_with_rag(case, retrieved if config.rag_enabled else [])

    t0 = time.perf_counter()
    try:
        verdict = judge.evaluate(summary)
        predicted = verdict.decision
        reason = verdict.reason
        err = ""
    except Exception as exc:  # noqa: BLE001 — eval only
        predicted = "ERROR"
        reason = ""
        err = f"{type(exc).__name__}: {exc}"
    duration_ms = (time.perf_counter() - t0) * 1000.0

    return CaseResult(
        cid=case.cid,
        config_slug=config.slug,
        expected=case.expected_decision,
        predicted=predicted,
        correct=(predicted == case.expected_decision),
        duration_ms=duration_ms,
        retrieved_chunk_ids=retrieved,
        retrieval_recall=recall,
        raw_reason=reason[:200],
        error=err,
    )


def _skip_reason(config: Configuration) -> str:
    if config.judge == "local-phi":
        path = os.environ.get("AEGIS_JUDGE_MODEL_PATH", "")
        if not path or not Path(path).is_file():
            return f"AEGIS_JUDGE_MODEL_PATH not set / file missing ({path!r})"
    if config.judge == "haiku" and not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY not set in env"
    return ""


def run_configuration(
    config: Configuration, cases: Iterable[BenchmarkCase],
) -> ConfigurationReport:
    skip = _skip_reason(config)
    if skip:
        return ConfigurationReport(
            config=config, skipped=True, skip_reason=skip,
        )

    _set_provider(config)
    judge = _build_judge(config)
    if judge is None:
        return ConfigurationReport(
            config=config, skipped=True,
            skip_reason="judge construction returned None",
        )

    t0 = time.perf_counter()
    results = [run_case(judge, config, c) for c in cases]
    total = (time.perf_counter() - t0) * 1000.0
    return ConfigurationReport(
        config=config, skipped=False, results=results, total_ms=total,
    )


DEFAULT_CONFIGURATIONS: tuple[Configuration, ...] = (
    Configuration(
        name="dummy-norag", judge="dummy", rag_enabled=False,
        description="DummyJudge baseline — heuristic regex, no RAG context.",
    ),
    Configuration(
        name="dummy-rag", judge="dummy", rag_enabled=True,
        description="DummyJudge with RAG block prepended (sanity check).",
    ),
    Configuration(
        name="sllm-norag", judge="local-phi", rag_enabled=False,
        description="LocalPhiJudge (TinyLlama / Llama-3.2-1B), no RAG.",
    ),
    Configuration(
        name="sllm-rag", judge="local-phi", rag_enabled=True,
        description="LocalPhiJudge with RAG block — main configuration.",
    ),
    Configuration(
        name="haiku-norag", judge="haiku", rag_enabled=False,
        description="Anthropic Haiku, no RAG (cloud baseline).",
    ),
    Configuration(
        name="haiku-rag", judge="haiku", rag_enabled=True,
        description="Anthropic Haiku with RAG — production configuration.",
    ),
)
