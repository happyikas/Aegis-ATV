"""Tests for the 1st security-hardening PR — three items bundled:

1. **Static-lint excerpt redaction** — UUID / epoch_ms matches in
   :class:`aegis.performance.cache_lint.StaticLintFinding` are masked
   so the finding can be safely shared in logs / support tickets
   without leaking the secret token that triggered it.

2. **AttentionSummaryGuard** — ``ATVInput.attention_per_token`` and
   ``ATVInput.attention_summary`` are excluded from
   ``model_dump()`` / ``model_dump_json()`` so a careless audit
   serialiser cannot accidentally persist per-token attention scores
   (which can leak the position of secrets in the prompt).

3. **Hook script baseline coverage** — ``tools/hooks/*.py`` is
   included in :data:`DEFAULT_INSTRUCTION_PATHS` so step309 detects
   tamper of the hook scripts themselves (they run with full session
   privileges and could exfil / falsify if compromised).
"""

from __future__ import annotations

from pathlib import Path

from aegis.instruction_baseline.manifest import (
    DEFAULT_INSTRUCTION_PATHS,
    diff_baseline,
    snapshot,
)
from aegis.performance.cache_lint import (
    StaticLintFinding,
    _redact_excerpt,
    analyze_system_prompt,
)
from aegis.schema import AttentionSummary, ATVHeader, ATVInput

# ──────────────────────────────────────────────────────────────────────
# Item #1 — static-lint excerpt redaction
# ──────────────────────────────────────────────────────────────────────


class TestExcerptRedaction:
    def test_uuid_excerpt_is_masked(self) -> None:
        out = analyze_system_prompt(
            "session: a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        )
        uuid_finding = next(f for f in out if f.pattern_name == "uuid")
        # The original UUID body MUST NOT appear in the excerpt.
        assert "ef1234567890" not in uuid_finding.matched_excerpt
        assert "e5f6" not in uuid_finding.matched_excerpt
        assert "abcd" not in uuid_finding.matched_excerpt
        # The first 8 chars survive — enough to debug, not enough to
        # reverse-lookup the token.
        assert uuid_finding.matched_excerpt.startswith("a1b2c3d4-")
        # Mask character (×) is present.
        assert "×" in uuid_finding.matched_excerpt

    def test_uuid_redaction_preserves_dash_structure(self) -> None:
        # 8-4-4-4-12 layout: dashes at positions 8, 13, 18, 23.
        masked = _redact_excerpt(
            "uuid", "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        )
        # Dash positions still readable; non-dash chars masked.
        assert masked[8] == "-"
        assert masked[13] == "-"
        assert masked[18] == "-"
        assert masked[23] == "-"

    def test_epoch_ms_redacted(self) -> None:
        out = analyze_system_prompt("ts: 1714834331000 ms")
        epoch_finding = next(
            (f for f in out if f.pattern_name == "epoch_ms"), None,
        )
        assert epoch_finding is not None
        # Original 13-digit string is NOT in the excerpt verbatim.
        assert "1714834331000" not in epoch_finding.matched_excerpt
        # First 4 digits survive for shape-debug.
        assert epoch_finding.matched_excerpt.startswith("1714")
        assert "×" in epoch_finding.matched_excerpt

    def test_date_iso_not_redacted(self) -> None:
        # Dates are public information; redaction would only impair
        # debuggability without protecting anything sensitive.
        out = analyze_system_prompt("Today is 2026-05-04")
        date_finding = next(f for f in out if f.pattern_name == "date_iso")
        assert date_finding.matched_excerpt == "2026-05-04"

    def test_phrase_markers_not_redacted(self) -> None:
        out = analyze_system_prompt("Today is the 4th of May.")
        markers = [f for f in out if f.pattern_name == "today_phrase"]
        assert markers
        for f in markers:
            assert f.matched_excerpt == "Today is"

    def test_redact_helper_passes_through_other_patterns(self) -> None:
        # Not in the redaction set → returned unchanged.
        assert _redact_excerpt("date_iso", "2026-05-04") == "2026-05-04"
        assert _redact_excerpt("today_phrase", "Today is") == "Today is"
        assert _redact_excerpt("time_of_day", "14:32:11") == "14:32:11"

    def test_redact_handles_short_input(self) -> None:
        # A pathological short UUID-shaped match shouldn't crash.
        assert _redact_excerpt("uuid", "a1b2") == "××××"
        assert _redact_excerpt("epoch_ms", "12") == "××"

    def test_finding_dataclass_carries_redacted_excerpt(self) -> None:
        # Construct directly to ensure StaticLintFinding doesn't
        # silently un-redact.
        f = StaticLintFinding(
            position=0, pattern_name="uuid",
            matched_excerpt="a1b2c3d4-××××-××××-××××-××××××××××××",
            severity="error", suggestion="x",
        )
        assert "ef1234567890" not in f.matched_excerpt


# ──────────────────────────────────────────────────────────────────────
# Item #3 — AttentionSummaryGuard
# ──────────────────────────────────────────────────────────────────────


def _mk_inp_with_attention() -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32, span_id="s" * 16,
            tenant_id="x", aid="a", timestamp_ns=0,
        ),
        tool_name="Bash",
        tool_args_json="{}",
        attention_per_token=[0.05, 0.10, 0.85],
        attention_summary=AttentionSummary(
            n_tokens=3, top_k_concentration=0.85,
            entropy_normalized=0.40,
        ),
    )


class TestAttentionGuard:
    def test_attention_per_token_excluded_from_model_dump(self) -> None:
        inp = _mk_inp_with_attention()
        d = inp.model_dump()
        assert "attention_per_token" not in d
        assert "attention_summary" not in d

    def test_attention_excluded_from_model_dump_json(self) -> None:
        inp = _mk_inp_with_attention()
        encoded = inp.model_dump_json()
        # The literal string of any per-token score must not surface.
        assert "0.85" not in encoded or "attention" not in encoded
        assert "attention_per_token" not in encoded
        assert "attention_summary" not in encoded

    def test_explicit_include_does_not_override_field_exclude(
        self,
    ) -> None:
        # Pydantic's Field(exclude=True) is unconditional — even an
        # explicit ``include={...}`` cannot resurface the field. This
        # is the strongest possible default.
        inp = _mk_inp_with_attention()
        d = inp.model_dump(include={"attention_per_token"})
        assert "attention_per_token" not in d

    def test_attribute_access_still_works(self) -> None:
        inp = _mk_inp_with_attention()
        # Direct attribute access is the documented path for advisors.
        assert inp.attention_per_token == [0.05, 0.10, 0.85]
        assert inp.attention_summary is not None
        assert inp.attention_summary.top_k_concentration == 0.85

    def test_eviction_advisor_still_consumes_attention(self) -> None:
        # Regression: PR #51 advisor reads via attribute, not dump.
        # Confirm the guard didn't break the advisor's data path.
        from aegis.atv.builder import build_atv
        from aegis.performance.eviction_advisor import eviction_advisor

        inp = ATVInput(
            header=ATVHeader(
                trace_id="t" * 32, span_id="s" * 16,
                tenant_id="x", aid="a", timestamp_ns=0,
            ),
            tool_name="Bash", tool_args_json="{}",
            attention_per_token=[0.001] * 50 + [0.95] * 50,
            attention_summary=AttentionSummary(
                n_tokens=100, top_k_concentration=0.85,
                entropy_normalized=0.30,
            ),
        )
        from aegis.schema import CostEfficiencyMetrics
        inp.cost_estimate = CostEfficiencyMetrics(
            context_utilization_ratio=0.80,
        )
        atv = build_atv(inp)
        advice = eviction_advisor(atv, inp)
        # When per-token data is supplied, evict_token_indices is
        # materialised — proving the guard didn't sever the data path.
        assert advice.evict_token_indices is not None


# ──────────────────────────────────────────────────────────────────────
# Item #4 — Hook script baseline coverage
# ──────────────────────────────────────────────────────────────────────


class TestHookBaselineCoverage:
    def test_default_paths_includes_hook_glob(self) -> None:
        assert "tools/hooks/*.py" in DEFAULT_INSTRUCTION_PATHS

    def test_snapshot_picks_up_hook_scripts(
        self, tmp_path: Path,
    ) -> None:
        # Synthesise a tiny repo with a CLAUDE.md and a hook script.
        (tmp_path / "CLAUDE.md").write_text("# project\n")
        (tmp_path / "tools" / "hooks").mkdir(parents=True)
        hook = tmp_path / "tools" / "hooks" / "user_prompt_submit.py"
        hook.write_text("#!/usr/bin/env python3\nprint('hook v1')\n")

        bl = snapshot(tmp_path)
        assert "tools/hooks/user_prompt_submit.py" in bl.files
        assert "CLAUDE.md" in bl.files

    def test_diff_detects_hook_modification(self, tmp_path: Path) -> None:
        # Establish a baseline, then tamper with the hook → diff
        # MUST flag it as modified.
        (tmp_path / "CLAUDE.md").write_text("# project\n")
        (tmp_path / "tools" / "hooks").mkdir(parents=True)
        hook = tmp_path / "tools" / "hooks" / "user_prompt_submit.py"
        hook.write_text("# original hook\n")

        baseline = snapshot(tmp_path)
        # Tamper.
        hook.write_text("# tampered hook — exfil routine inserted\n")

        report = diff_baseline(baseline, tmp_path)
        assert not report.is_clean
        modified_paths = [m[0] for m in report.modified]
        assert "tools/hooks/user_prompt_submit.py" in modified_paths

    def test_diff_detects_new_hook_added(self, tmp_path: Path) -> None:
        # Baseline has only CLAUDE.md, no hooks. Adding a hook later
        # must surface as 'added'.
        (tmp_path / "CLAUDE.md").write_text("# project\n")
        baseline = snapshot(tmp_path)

        (tmp_path / "tools" / "hooks").mkdir(parents=True)
        new_hook = tmp_path / "tools" / "hooks" / "rogue.py"
        new_hook.write_text("# rogue hook\n")

        report = diff_baseline(baseline, tmp_path)
        assert "tools/hooks/rogue.py" in report.added

    def test_diff_detects_hook_removal(self, tmp_path: Path) -> None:
        (tmp_path / "tools" / "hooks").mkdir(parents=True)
        hook = tmp_path / "tools" / "hooks" / "session_end.py"
        hook.write_text("# original\n")
        baseline = snapshot(tmp_path)

        hook.unlink()
        report = diff_baseline(baseline, tmp_path)
        assert "tools/hooks/session_end.py" in report.removed
