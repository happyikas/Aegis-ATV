"""v0.5.17 — plumbing tests for aid + knowledge_context through
the production advisor path (``aegis.judge.advisor``).

v0.5.16 wired ``aegis.judge.action_advice_sllm`` (a structured-
output composer), but the production firewall hook uses the
older ``aegis.judge.advisor.compose_advice_sllm`` dispatcher
which routes through ``Advisor.advise()`` (DummyAdvisor /
HaikuAdvisor). v0.5.17 plumbs the same wiki-context wiring into
that production path.

Three things to verify:

1. The :class:`Advisor` protocol now accepts ``knowledge_context``
   and ``DummyAdvisor`` / ``HaikuAdvisor`` both honour it.
2. ``_build_user_message`` includes the knowledge block at the
   top of the message when supplied, and stays byte-identical to
   v0.5.16 when not.
3. ``compose_advice_sllm`` (the dispatcher in advisor.py) opts
   into the wiki via ``aid`` + ``use_knowledge``, defaulting off,
   respecting ``AEGIS_ADVISOR_USE_KNOWLEDGE=1``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aegis.judge.advisor import (
    DummyAdvisor,
    _build_user_message,
    compose_advice_sllm,
)
from aegis.knowledge import (
    EntryKind,
    InfoBox,
    KnowledgeEntry,
    clear_advisor_cache,
    save_entry,
    save_index,
)

# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────


def _populate_wiki(tmp_path: Path, aid: str = "agent-A") -> None:
    """Build a minimal two-entry wiki the helper can resolve."""
    agent = KnowledgeEntry(
        entry_id=f"agent/{aid}",
        kind=EntryKind.AGENT,
        title=f"Agent {aid}",
        summary="Test agent — 100 calls, 95% allow rate.",
        infobox=InfoBox(fields={"n_calls": 100, "n_block": 5}),
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
    save_index(
        [agent, tool], root=tmp_path,
        built_at_ns=1, built_from_records=1,
    )


# ──────────────────────────────────────────────────────────────────
# 1. Advisor protocol accepts new kwarg
# ──────────────────────────────────────────────────────────────────


class TestAdvisorProtocolAcceptsKnowledge:
    def test_dummy_ignores_knowledge_context_silently(self) -> None:
        """The deterministic heuristic shouldn't use the wiki, but
        the protocol must accept the kwarg without raising."""
        advice = DummyAdvisor().advise(
            base_decision="ALLOW",
            base_reason="test",
            current_tool="Bash",
            knowledge_context="# Agent foo\nbackground info",
        )
        # The advice itself is unaffected — heuristic is signal-driven.
        assert advice.advisor_kind != "sllm-haiku"


# ──────────────────────────────────────────────────────────────────
# 2. _build_user_message includes knowledge block
# ──────────────────────────────────────────────────────────────────


class TestBuildUserMessageIncludesKnowledge:
    def test_no_knowledge_no_block(self) -> None:
        msg = _build_user_message(
            temporal_ctx=None,
            anomalies=None,
            baseline=None,
            catalog=None,
            intent_classifier=None,
            action_table=None,
            base_decision="ALLOW",
            base_reason="",
            current_tool="Bash",
        )
        assert "KNOWLEDGE CONTEXT" not in msg

    def test_with_knowledge_block_at_top(self) -> None:
        msg = _build_user_message(
            temporal_ctx=None,
            anomalies=None,
            baseline=None,
            catalog=None,
            intent_classifier=None,
            action_table=None,
            base_decision="ALLOW",
            base_reason="",
            current_tool="Bash",
            knowledge_context="# Agent foo\nthe agent background",
        )
        assert "KNOWLEDGE CONTEXT" in msg
        assert "agent background" in msg
        assert "END KNOWLEDGE CONTEXT" in msg
        # The knowledge block must come BEFORE the PROPOSED CALL
        # section (which is the last block we always append).
        kc_pos = msg.index("KNOWLEDGE CONTEXT")
        proposed_pos = msg.index("PROPOSED CALL")
        assert kc_pos < proposed_pos


# ──────────────────────────────────────────────────────────────────
# 3. compose_advice_sllm dispatcher opts in correctly
# ──────────────────────────────────────────────────────────────────


class _CapturingAdvisor:
    """Stub :class:`Advisor` that records its received kwargs so the
    test can assert what got plumbed through."""

    advisor_kind = "stub"

    def __init__(self) -> None:
        self.received: dict[str, Any] = {}

    def advise(self, **kwargs: Any) -> Any:
        self.received = kwargs
        # Return a minimal ActionAdvice-compatible object.
        from aegis.judge.action_advice import compose_advice_heuristic
        return compose_advice_heuristic(
            base_decision="ALLOW",
            base_reason="stub",
            current_tool=kwargs.get("current_tool", ""),
        )


class TestComposeAdviceSllmDispatcher:
    def setup_method(self) -> None:
        clear_advisor_cache()

    def test_default_off_no_knowledge(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_wiki(tmp_path)
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.delenv("AEGIS_ADVISOR_USE_KNOWLEDGE", raising=False)

        capt = _CapturingAdvisor()
        compose_advice_sllm(
            base_decision="ALLOW",
            current_tool="Bash",
            aid="agent-A",
            advisor=capt,
        )
        assert capt.received.get("knowledge_context") is None

    def test_env_opt_in_loads_wiki(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_wiki(tmp_path)
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "1")

        capt = _CapturingAdvisor()
        compose_advice_sllm(
            base_decision="ALLOW",
            current_tool="Bash",
            aid="agent-A",
            advisor=capt,
        )
        ctx = capt.received.get("knowledge_context")
        assert ctx is not None
        assert "Agent agent-A" in ctx
        assert "Tool Bash" in ctx  # cross-ref followed

    def test_explicit_kwarg_overrides_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_wiki(tmp_path)
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "1")

        capt = _CapturingAdvisor()
        compose_advice_sllm(
            base_decision="ALLOW",
            current_tool="Bash",
            aid="agent-A",
            advisor=capt,
            use_knowledge=False,  # explicit override
        )
        assert capt.received.get("knowledge_context") is None

    def test_opt_in_without_aid_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Opt-in flag set but no aid → no knowledge block (safe)."""
        _populate_wiki(tmp_path)
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "1")

        capt = _CapturingAdvisor()
        compose_advice_sllm(
            base_decision="ALLOW",
            current_tool="Bash",
            advisor=capt,
        )
        assert capt.received.get("knowledge_context") is None

    def test_opt_in_unknown_aid_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _populate_wiki(tmp_path, aid="agent-A")
        monkeypatch.setenv("AEGIS_KNOWLEDGE_DIR", str(tmp_path))
        monkeypatch.setenv("AEGIS_ADVISOR_USE_KNOWLEDGE", "1")

        capt = _CapturingAdvisor()
        compose_advice_sllm(
            base_decision="ALLOW",
            current_tool="Bash",
            aid="other-agent",
            advisor=capt,
        )
        assert capt.received.get("knowledge_context") is None
