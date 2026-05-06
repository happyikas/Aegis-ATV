"""Tests for ``demo.sllm_rag_eval`` — schema + dummy-mode end-to-end."""
from __future__ import annotations

from pathlib import Path

import pytest
from demo.sllm_rag_eval.cases import BenchmarkCase, cases
from demo.sllm_rag_eval.render import (
    render_markdown_report,
    render_terminal_summary,
)
from demo.sllm_rag_eval.runner import (
    DEFAULT_CONFIGURATIONS,
    Configuration,
    run_configuration,
)

# ── Schema ────────────────────────────────────────────────────────────


def test_30_cases_total() -> None:
    assert len(cases()) == 30


def test_balanced_decision_distribution() -> None:
    cs = cases()
    decisions = [c.expected_decision for c in cs]
    assert decisions.count("ALLOW") == 10
    assert decisions.count("BLOCK") == 10
    assert decisions.count("REQUIRE_APPROVAL") == 10


def test_unique_case_ids() -> None:
    cs = cases()
    ids = [c.cid for c in cs]
    assert len(ids) == len(set(ids))


def test_difficulty_labels_valid() -> None:
    for c in cases():
        assert c.difficulty in ("easy", "medium", "hard")


def test_each_block_case_has_chunk_id_hint() -> None:
    """BLOCK cases should know which chunk RAG ought to retrieve —
    that's how we measure retrieval recall."""
    block_cases = [c for c in cases() if c.expected_decision == "BLOCK"]
    for c in block_cases:
        assert c.expected_chunk_ids, (
            f"BLOCK case {c.cid} missing expected_chunk_ids"
        )


def test_summary_text_present_and_under_500_chars() -> None:
    for c in cases():
        assert c.summary
        assert len(c.summary) < 500, (
            f"{c.cid}: summary {len(c.summary)} chars exceeds budget"
        )


# ── Configurations ────────────────────────────────────────────────────


def test_default_configurations_cover_all_pairs() -> None:
    slugs = {c.slug for c in DEFAULT_CONFIGURATIONS}
    expected = {
        "dummy-norag", "dummy-rag",
        "local-phi-norag", "local-phi-rag",
        "haiku-norag", "haiku-rag",
    }
    assert slugs == expected


def test_configuration_slug_format() -> None:
    for cfg in DEFAULT_CONFIGURATIONS:
        rag_part = "rag" if cfg.rag_enabled else "norag"
        assert cfg.slug == f"{cfg.judge}-{rag_part}"


# ── Runner — dummy mode end-to-end ────────────────────────────────────


def test_run_dummy_norag_returns_30_results() -> None:
    cfg = Configuration(name="t", judge="dummy", rag_enabled=False)
    rep = run_configuration(cfg, cases())
    assert not rep.skipped
    assert rep.n_total == 30
    # DummyJudge is deterministic — same input always produces same output.
    rep2 = run_configuration(cfg, cases())
    decisions_1 = [r.predicted for r in rep.results]
    decisions_2 = [r.predicted for r in rep2.results]
    assert decisions_1 == decisions_2


def test_run_dummy_rag_retrieves_chunks() -> None:
    cfg = Configuration(name="t", judge="dummy", rag_enabled=True)
    rep = run_configuration(cfg, cases())
    assert not rep.skipped
    # Every case with RAG enabled gets at least 1 retrieved chunk.
    for r in rep.results:
        assert len(r.retrieved_chunk_ids) >= 1, (
            f"{r.cid}: expected ≥1 retrieved chunk under dummy-rag, "
            f"got {r.retrieved_chunk_ids}"
        )


def test_skipped_local_phi_when_model_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AEGIS_JUDGE_MODEL_PATH", raising=False)
    cfg = Configuration(name="t", judge="local-phi", rag_enabled=True)
    rep = run_configuration(cfg, cases()[:1])
    assert rep.skipped
    assert "AEGIS_JUDGE_MODEL_PATH" in rep.skip_reason


def test_skipped_haiku_when_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = Configuration(name="t", judge="haiku", rag_enabled=True)
    rep = run_configuration(cfg, cases()[:1])
    assert rep.skipped
    assert "ANTHROPIC_API_KEY" in rep.skip_reason


# ── Render ────────────────────────────────────────────────────────────


def test_render_terminal_summary_with_skipped_only() -> None:
    """Even when every config is skipped, render must produce sane output."""
    from demo.sllm_rag_eval.runner import ConfigurationReport
    cfg = Configuration(name="t", judge="haiku", rag_enabled=False)
    rep = ConfigurationReport(config=cfg, skipped=True, skip_reason="no key")
    out = render_terminal_summary([rep])
    assert "Skipped" in out
    assert "haiku-norag" in out


def test_render_markdown_report_includes_analysis_when_runnable() -> None:
    cfg = Configuration(name="t", judge="dummy", rag_enabled=False)
    rep = run_configuration(cfg, cases()[:5])
    md = render_markdown_report(cases()[:5], [rep])
    assert "# Aegis RAG + sLLM 30-Case Benchmark Report" in md
    assert "## Headline accuracy" in md
    assert "## Analysis" in md
    assert "Best accuracy" in md


# ── silence unused imports ────────────────────────────────────────────
_ = (Path, BenchmarkCase)
