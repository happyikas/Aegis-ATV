"""Step 309 — Instruction baseline drift detector (v2.2.1, Day-1 #3).

Sits right after step305 (safe allowlist) and before step310 (arg
inspection). On every PreToolUse, re-hashes the tracked instruction
files (CLAUDE.md, AGENTS.md, .mcp.json, plugin/skill manifests…) and
compares against the baseline at ``settings.aegis_instruction_baseline_path``.

If any file is added / removed / modified relative to the baseline,
this step short-circuits the pipeline with BLOCK. The agent stays
blocked on every subsequent call until either:

* the user re-runs ``aegis baseline reattest`` (snapshots the new
  state and writes a fresh manifest), or
* the user reverts the offending file.

This is the "configuration mutation monitoring" surface of the AIA
patent — it catches both repository-resident instruction poisoning
(a malicious commit silently appending to CLAUDE.md) and runtime
mutation (an agent that learned to write to its own AGENTS.md).

Disabled by default: the firewall does nothing if
``aegis_instruction_baseline_path`` is empty. ``aegis baseline init``
populates it. For the sidecar service we leave it empty so the M1–M17
test surface is unaffected; local-mode plugin install (Phase 5) is
where the baseline gets wired up by default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

from aegis.config import settings
from aegis.firewall.core import FirewallContext, StepResult
from aegis.instruction_baseline import (
    InstructionBaseline,
    diff_baseline,
    load_baseline,
)
from aegis.schema import ATVInput


@lru_cache(maxsize=4)
def _cached_baseline(path_str: str) -> InstructionBaseline | None:
    """Cache the loaded manifest. ``aegis baseline reattest`` should
    drop this cache via :func:`reset_baseline_cache` after rewriting
    the file."""
    p = Path(path_str)
    if not p.exists():
        return None
    return load_baseline(p)


def reset_baseline_cache() -> None:
    """Test helper / called by ``aegis baseline reattest``."""
    _cached_baseline.cache_clear()


def run(
    atv: np.ndarray, inp: ATVInput, ctx: FirewallContext
) -> StepResult:
    baseline_path_str = settings.aegis_instruction_baseline_path
    if not baseline_path_str:
        return StepResult(
            verdict=None, reason="", trace="step309: baseline disabled"
        )

    baseline = _cached_baseline(baseline_path_str)
    if baseline is None:
        return StepResult(
            verdict=None,
            reason="",
            trace=f"step309: no baseline at {baseline_path_str}",
        )

    root = Path(settings.aegis_instruction_root or baseline.root or ".")
    report = diff_baseline(baseline, root)
    if report.is_clean:
        return StepResult(
            verdict=None, reason="", trace="step309: baseline intact"
        )

    ctx.extras["instruction_drift"] = {
        "added": report.added,
        "removed": report.removed,
        "modified": [m[0] for m in report.modified],
        # PR-E — surface model-weight drift separately so forensic
        # tooling and `aegis report --explain` can render the higher-
        # severity category distinctly.
        "added_weights": report.added_weights,
        "removed_weights": report.removed_weights,
        "modified_weights": [m[0] for m in report.modified_weights],
    }
    summary = report.summary()

    # PR-E — model-weight drift is a higher-severity signal (true
    # supply-chain attack vector for self-hosted LLM deployments) so
    # we promote it to its own reason category rather than burying it
    # in the generic "instruction_drift" bucket. Severity ordering is
    # preserved when both categories drift simultaneously: model
    # weights take precedence in the reason string.
    if report.has_model_drift:
        weight_files: list[str] = []
        weight_files.extend(report.added_weights)
        weight_files.extend(report.removed_weights)
        weight_files.extend(m[0] for m in report.modified_weights)
        head = ", ".join(weight_files[:3])
        tail = "…" if len(weight_files) > 3 else ""
        return StepResult(
            verdict="BLOCK",
            reason=(
                f"model_weight_drift: {summary} ({head}{tail}) — "
                "run `aegis baseline reattest --include-model-weights` "
                "if this is a deliberate model upgrade"
            ),
            trace=f"step309: model weight drift — {summary}",
        )

    drift_files: list[str] = []
    drift_files.extend(report.added)
    drift_files.extend(report.removed)
    drift_files.extend(m[0] for m in report.modified)
    return StepResult(
        verdict="BLOCK",
        reason=(
            f"instruction_drift: {summary} ({', '.join(drift_files[:3])}"
            f"{'…' if len(drift_files) > 3 else ''})"
        ),
        trace=f"step309: baseline drift — {summary}",
    )
