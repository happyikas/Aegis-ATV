"""End-to-end test: real local sLLM emits a valid verdict.

Skips automatically when:

* ``llama-cpp-python`` is not installed (CI / containers without it).
* No GGUF is present in ``./models/`` (fresh checkout pre-``pull-model``).

When both are present (Mac mini after ``aegis pull-model`` +
``uv sync --extra local-llm``), this test runs an actual Llama
inference and verifies the contract that step340 / HybridJudge
relies on:

1. ``LocalPhiJudge._decide_mode()`` returns ``"real"`` (not stub).
2. ``evaluate_full()`` returns a valid 3-class decision.
3. ``model_hash`` is the file SHA3 (audit-replayable).
4. ``latency_ms`` is positive and under 5s (Claude Code hook timeout).
5. The verdict is deterministic — same input twice → same output.

This is the test that proves "Solo Free actually invokes a real LLM"
end-to-end. Without it, the system can silently regress to stub mode
and pass the rest of the suite.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

from aegis.atv.builder import build_atv
from aegis.judge.local_phi import LocalPhiJudge, reset_model_hash_cache
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics


def _llama_cpp_installed() -> bool:
    return importlib.util.find_spec("llama_cpp") is not None


def _find_gguf() -> Path | None:
    """Find any GGUF in the repo's ``models/`` directory."""
    repo_root = Path(__file__).resolve().parents[2]
    models_dir = repo_root / "models"
    if not models_dir.exists():
        return None
    for p in sorted(models_dir.glob("*.gguf")):
        if p.is_file() and p.stat().st_size > 100_000_000:  # >100 MB → real
            return p
    return None


pytestmark = [
    pytest.mark.skipif(
        not _llama_cpp_installed(),
        reason="llama-cpp-python not installed (uv sync --extra local-llm)",
    ),
    pytest.mark.skipif(
        _find_gguf() is None,
        reason="no GGUF in models/ (run: aegis pull-model)",
    ),
]


@pytest.fixture
def real_local_phi(monkeypatch: pytest.MonkeyPatch) -> LocalPhiJudge:
    """Configure LocalPhiJudge for real-model mode."""
    gguf = _find_gguf()
    assert gguf is not None  # the skipif gate guarantees this
    monkeypatch.setenv("AEGIS_JUDGE_MODEL_PATH", str(gguf))
    monkeypatch.delenv("AEGIS_JUDGE_LOCAL_PHI_STUB", raising=False)
    reset_model_hash_cache()
    return LocalPhiJudge()


def _atv_input(tool: str, args: str) -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t-real", span_id="s-real",
            tenant_id="solo-free", aid="dogfood",
            timestamp_ns=time.time_ns(),
        ),
        agent_state_text="user request",
        plan_text=f"call {tool}",
        tool_name=tool,
        tool_args_json=args,
        safety_flags={},
        memory_fingerprint="sha3:test",
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=10, output_token_count=5,
        ),
    )


def test_real_mode_is_active(real_local_phi: LocalPhiJudge) -> None:
    """The judge must enter ``real`` mode when GGUF + llama-cpp present.

    This is the canary: if this test fails, the rest of the file is
    silently testing stub mode and we'd have no real-LLM coverage.
    """
    mode, info = real_local_phi._decide_mode()
    assert mode == "real", (
        f"expected real mode, got {mode!r} (info: {info!r}) — model file "
        "may be corrupt or llama-cpp build broken"
    )


def test_real_verdict_is_valid_3class(real_local_phi: LocalPhiJudge) -> None:
    """Real LLM must emit one of the three allowed decisions."""
    inp = _atv_input("Bash", '{"command":"ls"}')
    atv = build_atv(inp)
    v = real_local_phi.evaluate_full(
        'tool=Bash command="ls"', atv=atv, inp=inp,
    )
    assert v.decision in {"ALLOW", "BLOCK", "REQUIRE_APPROVAL"}, (
        f"verdict outside 3-class enum: {v.decision!r}"
    )


def test_model_hash_matches_file_sha3(real_local_phi: LocalPhiJudge) -> None:
    """Audit-replay invariant: ``model_hash == SHA3-256(GGUF file)``."""
    import hashlib
    inp = _atv_input("Bash", '{"command":"ls"}')
    atv = build_atv(inp)
    v = real_local_phi.evaluate_full("ls", atv=atv, inp=inp)

    gguf = _find_gguf()
    assert gguf is not None
    h = hashlib.sha3_256()
    with gguf.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    assert v.model_hash == h.hexdigest(), (
        "real-mode verdict's model_hash must equal the GGUF file SHA3 "
        "so aegis verify-audit can replay against the correct binary"
    )


def test_real_verdict_under_5s_budget(real_local_phi: LocalPhiJudge) -> None:
    """Claude Code times out hooks at 5s. First-call cold load + inference
    must fit comfortably under that on a Mac mini."""
    inp = _atv_input("Bash", '{"command":"ls"}')
    atv = build_atv(inp)
    v = real_local_phi.evaluate_full("ls", atv=atv, inp=inp)
    assert v.latency_ms > 0
    assert v.latency_ms < 5000, (
        f"latency {v.latency_ms} ms exceeds Claude Code's 5s hook timeout"
    )


def test_real_verdict_deterministic_same_input(
    real_local_phi: LocalPhiJudge,
) -> None:
    """Greedy decode (T=0, top_k=1) must give bit-identical output for
    the same input — required for audit replay."""
    inp = _atv_input("Bash", '{"command":"ls"}')
    atv = build_atv(inp)
    v1 = real_local_phi.evaluate_full("ls", atv=atv, inp=inp)
    v2 = real_local_phi.evaluate_full("ls", atv=atv, inp=inp)
    assert v1.decision == v2.decision
    assert v1.reason == v2.reason
    assert v1.model_hash == v2.model_hash


def test_real_verdict_blocks_obvious_destructive_via_hybrid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full hybrid cascade (M13 → LocalPhi → Dummy) must protect
    against ``rm -rf`` even though Llama-1B alone might miss it.

    This is the deepest contract: M13 catches Tier 1 in <1ms, LLM is
    only escalation. Solo Free user must never see ALLOW on rm -rf /.

    Uses ``monkeypatch`` (not direct ``os.environ`` mutation) so the
    singleton ``aegis.config.settings`` is restored after the test —
    otherwise downstream tests in the same session see judge=hybrid
    instead of the default and fail spuriously.
    """
    gguf = _find_gguf()
    assert gguf is not None
    monkeypatch.setenv("AEGIS_JUDGE_MODEL_PATH", str(gguf))
    monkeypatch.setenv("AEGIS_JUDGE_PROVIDER", "hybrid")
    monkeypatch.setenv("AEGIS_EMBEDDING_PROVIDER", "dummy")
    monkeypatch.delenv("AEGIS_JUDGE_LOCAL_PHI_STUB", raising=False)

    # The settings singleton is frozen at import — mutate the live
    # attribute and let monkeypatch restore it on test teardown.
    from aegis.config import settings as _settings
    monkeypatch.setattr(_settings, "aegis_judge_provider", "hybrid")
    monkeypatch.setattr(_settings, "aegis_embedding_provider", "dummy")

    from aegis.judge import get_judge
    j = get_judge()
    inp = _atv_input("Bash", '{"command":"rm -rf /var/log/*"}')
    atv = build_atv(inp)
    v = j.evaluate_full(
        'tool=Bash command="rm -rf /var/log/*"', atv=atv, inp=inp,
    )
    assert v.decision in {"BLOCK", "REQUIRE_APPROVAL"}, (
        f"hybrid let through rm -rf — got {v.decision} "
        f"(reason: {v.reason})"
    )
