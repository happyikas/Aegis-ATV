"""Tests for v0.5.16 — sLLM advisors consume the knowledge wiki.

Three layers of behaviour to pin-test:

1. **Helper**: ``knowledge_context_for_advisor`` returns
   prompt-ready markdown when a wiki exists, ``None`` otherwise,
   and never raises.
2. **Prompt builders**: ``_build_sllm_prompt`` (TripleAxisAdvisor)
   and ``_build_prompt`` (ActionAdvice) include the knowledge
   block when supplied and stay byte-identical to v0.5.15 when
   not.
3. **Umbrella composers**: ``assess_triple_axis`` and
   ``compose_advice`` honour ``AEGIS_ADVISOR_USE_KNOWLEDGE=1`` +
   ``aid`` to opt into the knowledge path; default off preserves
   prior behaviour.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aegis.context_memory.record import ContextMemoryRecord
from aegis.judge.action_advice import ActionAdvice
from aegis.judge.action_advice_sllm import (
    _build_prompt,
    compose_advice,
)
from aegis.judge.triple_axis_advisor import (
    _build_sllm_prompt,
    assess_triple_axis,
    assess_via_heuristic,
    extract_axis_signals,
)
from aegis.knowledge import (
    EntryKind,
    InfoBox,
    KnowledgeEntry,
    advisor_knowledge_enabled,
    clear_advisor_cache,
    knowledge_context_for_advisor,
    save_entry,
    save_index,
)

# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────


def _mk_record(
    *,
    decision: str = "ALLOW",
    reason: str = "",
    tool: str = "Bash",
    aid: str = "agent-A",
    trace_id: str = "trace-001",
    ts_ns: int | None = None,
) -> ContextMemoryRecord:
    return ContextMemoryRecord(
        ts_ns=ts_ns if ts_ns is not None else time.time_ns(),
        trace_id=trace_id,
        invocation_id="inv-001",
        aid=aid,
        tenant_id="t",
        tool_name=tool,
        decision=decision,
        reason=reason,
        channel=None,
        provider=None,
        latency_ms=100.0,
        cost_usd=0.001,
        tokens_in=100,
        tokens_out=50,
        step_traces={},
        m13_score=None,
        advisor_invoked=False,
        recommended_advisors=(),
        atv_sha3=None,
        atv_dim=2080,
        is_sidechain=False,
        mode="sidecar",
    )


def _populate_wiki(tmp_path: Path, aid: str = "agent-A") -> None:
    """Build a minimal wiki: one agent entry + one tool entry,
    cross-referenced. Just enough that the advisor helper has
    something to render."""
    agent = KnowledgeEntry(
        entry_id=f"agent/{aid}",
        kind=EntryKind.AGENT,
        title=f"Agent {aid}",
        summary="Test agent — 100 calls, 95% allow rate.",
        infobox=InfoBox(fields={
            "n_calls": 100, "n_block": 5,
        }),
        related=("tool/Bash",),
        tags=("high-volume",),
        n_observations=100,
        confidence=1.0,
    )
    tool = KnowledgeEntry(
        entry_id="tool/Bash",
        kind=EntryKind.TOOL,
        title="Tool Bash",
        summary="Test tool — 200 invocations.",
        n_observations=200,
        confidence=1.0,
    )
    save_entry(agent, root=tmp_path)
    save_entry(tool, root=tmp_path)
    save_index([agent, tool], root=tmp_path, built_at_ns=1, built_from_records=1)


# ──────────────────────────────────────────────────────────────────
# 1. Helper
# ──────────────────────────────────────────────────────────────────


class TestKnowledgeContextHelper:
    def setup_method(self) -> None:
        clear_advisor_cache()

    def test_no_aid_returns_none(self, tmp_path: Path) -> None:
        assert knowledge_context_for_advisor(None, root=tmp_path) is None
        assert knowledge_context_for_advisor("", root=tmp_path) is None

    def test_missing_wiki_returns_none(self, tmp_path: Path) -> None:
        # tmp_path is empty — no index, no entries
        assert knowledge_context_for_advisor("agent-A", root=tmp_path) is None

    def test_with_wiki_returns_markdown(self, tmp_path: Path) -> None:
        _populate_wiki(tmp_path)
        ctx = knowledge_context_for_advisor("agent-A", root=tmp_path)
        assert ctx is not None
        assert "Agent agent-A" in ctx
        assert "Tool Bash" in ctx  # cross-ref followed
        assert "Knowledge" in ctx  # intro line

    def test_missing_aid_in_wiki_returns_none(self, tmp_path: Path) -> None:
        _populate_wiki(tmp_path, aid="agent-A")
        assert knowledge_context_for_advisor("other-agent", root=tmp_path) is None

    def test_env_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AEGIS_ADVISOR_USE_KNOWLEDGE", raising=False)
        assert advisor_knowledge_enabled() is False
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "1")
        assert advisor_knowledge_enabled() is True
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "yes")
        assert advisor_knowledge_enabled() is True
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "0")
        assert advisor_knowledge_enabled() is False

    def test_cache_warm_path(self, tmp_path: Path) -> None:
        """Second call with unchanged wiki should return the
        cached value (same object identity)."""
        _populate_wiki(tmp_path)
        ctx1 = knowledge_context_for_advisor("agent-A", root=tmp_path)
        ctx2 = knowledge_context_for_advisor("agent-A", root=tmp_path)
        assert ctx1 is ctx2  # identity, not just equality


# ──────────────────────────────────────────────────────────────────
# 2. Prompt builders
# ──────────────────────────────────────────────────────────────────


class TestTripleAxisPromptIncludesKnowledge:
    def test_no_knowledge_unchanged(self) -> None:
        signals = extract_axis_signals([_mk_record()])
        baseline = assess_via_heuristic(signals)
        prompt = _build_sllm_prompt(signals, baseline)
        assert "Knowledge context" not in prompt

    def test_with_knowledge_embedded(self) -> None:
        signals = extract_axis_signals([_mk_record()])
        baseline = assess_via_heuristic(signals)
        prompt = _build_sllm_prompt(
            signals, baseline,
            knowledge_context="# Agent foo\nSummary: test wiki",
        )
        assert "Knowledge context" in prompt
        assert "# Agent foo" in prompt
        assert "End knowledge context" in prompt

    def test_knowledge_appears_before_signals(self) -> None:
        signals = extract_axis_signals([_mk_record()])
        baseline = assess_via_heuristic(signals)
        prompt = _build_sllm_prompt(
            signals, baseline,
            knowledge_context="# Agent foo\nbackground info",
        )
        # The block lands between the instructions and the signals.
        knowledge_pos = prompt.index("Knowledge context")
        signals_pos = prompt.index("Signals:")
        assert knowledge_pos < signals_pos


class TestActionAdvicePromptIncludesKnowledge:
    def _baseline(self) -> ActionAdvice:
        from aegis.judge.action_advice import compose_advice_heuristic
        return compose_advice_heuristic(
            temporal_ctx=None,
            anomalies=[],
            base_decision="ALLOW",
            base_reason="test",
        )

    def test_no_knowledge_unchanged(self) -> None:
        prompt = _build_prompt(self._baseline(), current_tool="Bash")
        assert "Knowledge context" not in prompt

    def test_with_knowledge_embedded(self) -> None:
        prompt = _build_prompt(
            self._baseline(),
            current_tool="Bash",
            knowledge_context="# Agent foo\nsummary: test wiki",
        )
        assert "Knowledge context" in prompt
        assert "# Agent foo" in prompt

    def test_knowledge_before_heuristic_baseline(self) -> None:
        prompt = _build_prompt(
            self._baseline(),
            current_tool="Bash",
            knowledge_context="# Agent foo\nbackground",
        )
        kc = prompt.index("Knowledge context")
        hb = prompt.index("Heuristic baseline:")
        assert kc < hb


# ──────────────────────────────────────────────────────────────────
# 3. Umbrella composers honour opt-in + aid
# ──────────────────────────────────────────────────────────────────


class TestTripleAxisUmbrellaOptsIn:
    def setup_method(self) -> None:
        clear_advisor_cache()

    def test_default_off_no_knowledge_in_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_wiki(tmp_path)
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.delenv("AEGIS_ADVISOR_USE_KNOWLEDGE", raising=False)
        captured_prompt: dict[str, str] = {}

        def stub_llm(prompt: str) -> str | None:
            captured_prompt["prompt"] = prompt
            return None  # fall back to heuristic

        assess_triple_axis(
            [_mk_record(aid="agent-A")],
            prefer_sllm=True,
            llm_call=stub_llm,
            aid="agent-A",
        )
        assert "Knowledge context" not in captured_prompt.get("prompt", "")

    def test_opt_in_includes_knowledge(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_wiki(tmp_path)
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "1")
        captured: dict[str, str] = {}

        def stub_llm(prompt: str) -> str | None:
            captured["prompt"] = prompt
            return None

        assess_triple_axis(
            [_mk_record(aid="agent-A")],
            prefer_sllm=True,
            llm_call=stub_llm,
            aid="agent-A",
        )
        prompt = captured.get("prompt", "")
        assert "Knowledge context" in prompt
        assert "Agent agent-A" in prompt

    def test_opt_in_without_aid_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Opt-in flag set but no aid → no knowledge block (safe)."""
        _populate_wiki(tmp_path)
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "1")
        captured: dict[str, str] = {}

        def stub_llm(prompt: str) -> str | None:
            captured["prompt"] = prompt
            return None

        assess_triple_axis(
            [_mk_record(aid="agent-A")],
            prefer_sllm=True,
            llm_call=stub_llm,
            # aid intentionally omitted
        )
        assert "Knowledge context" not in captured.get("prompt", "")

    def test_explicit_kwarg_overrides_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_wiki(tmp_path)
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "1")  # env says yes
        captured: dict[str, str] = {}

        def stub_llm(prompt: str) -> str | None:
            captured["prompt"] = prompt
            return None

        # Explicit use_knowledge=False overrides.
        assess_triple_axis(
            [_mk_record(aid="agent-A")],
            prefer_sllm=True,
            llm_call=stub_llm,
            aid="agent-A",
            use_knowledge=False,
        )
        assert "Knowledge context" not in captured.get("prompt", "")


class TestActionAdviceUmbrellaOptsIn:
    def setup_method(self) -> None:
        clear_advisor_cache()

    def _kwargs(self) -> dict[str, object]:
        return {
            "temporal_ctx": None,
            "anomalies": [],
            "base_decision": "ALLOW",
            "base_reason": "test",
            "current_tool": "Bash",
        }

    def test_default_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_wiki(tmp_path)
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.delenv("AEGIS_ADVISOR_USE_KNOWLEDGE", raising=False)
        captured: dict[str, str] = {}

        def stub_llm(prompt: str) -> str | None:
            captured["prompt"] = prompt
            return None

        compose_advice(
            prefer_sllm=True,
            llm_call=stub_llm,
            aid="agent-A",
            **self._kwargs(),
        )
        assert "Knowledge context" not in captured.get("prompt", "")

    def test_opt_in_includes_knowledge(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_wiki(tmp_path)
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "1")
        captured: dict[str, str] = {}

        def stub_llm(prompt: str) -> str | None:
            captured["prompt"] = prompt
            return None

        compose_advice(
            prefer_sllm=True,
            llm_call=stub_llm,
            aid="agent-A",
            **self._kwargs(),
        )
        prompt = captured.get("prompt", "")
        assert "Knowledge context" in prompt
        assert "Agent agent-A" in prompt

    def test_heuristic_path_unaffected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``prefer_sllm=False`` the helper isn't consulted —
        knowledge_context is never built or read. Verifies the
        heuristic path's signature didn't accidentally get a
        knowledge kwarg that explodes."""
        _populate_wiki(tmp_path)
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "1")

        advice = compose_advice(
            prefer_sllm=False,  # heuristic
            aid="agent-A",
            use_knowledge=True,  # opt-in flag set but path is heuristic
            **self._kwargs(),
        )
        assert advice.advisor_kind != "sllm"


# ──────────────────────────────────────────────────────────────────
# 4. Hot-path safety
# ──────────────────────────────────────────────────────────────────


class TestHotPathSafety:
    """The advisors sit on the firewall hot path. None of the new
    code paths may raise — neither a corrupted wiki nor a missing
    knowledge dir is allowed to bubble up."""

    def setup_method(self) -> None:
        clear_advisor_cache()

    def test_helper_swallows_corrupted_index(
        self, tmp_path: Path,
    ) -> None:
        from aegis.knowledge.store import index_path
        (tmp_path / "knowledge").mkdir()
        index_path(tmp_path).write_text("not json", encoding="utf-8")
        # Should not raise; should return None.
        assert knowledge_context_for_advisor(
            "agent-A", root=tmp_path,
        ) is None

    def test_helper_swallows_missing_dir(self) -> None:
        bogus = Path("/nonexistent/path/that/does/not/exist")
        assert knowledge_context_for_advisor("agent-A", root=bogus) is None
