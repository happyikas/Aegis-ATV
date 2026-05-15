"""Tests for ``aegis.rules`` — Hookify-style user rules."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aegis.rules.matcher import any_blocking, evaluate
from aegis.rules.nl_to_regex import (
    suggest_regex,
    suggest_rule_name,
)
from aegis.rules.schema import (
    VALID_SEVERITIES,
    Rule,
    SchemaError,
    parse_markdown,
    serialize_markdown,
)
from aegis.rules.storage import (
    SOLO_FREE_RULE_LIMIT,
    RuleError,
    delete_rule,
    list_rules,
    load_rule,
    rules_dir,
    save_rule,
    set_enabled,
)

# ── schema ──────────────────────────────────────────────────────


def _make_rule(**over) -> Rule:  # type: ignore[no-untyped-def]
    base = {
        "name": "block-test",
        "severity": "critical",
        "enabled": True,
        "description": "test rule",
        "pattern": r"\btest\b",
        "message": "blocked",
        "source": "user",
    }
    base.update(over)
    return Rule(**base)


def test_rule_construction_ok() -> None:
    r = _make_rule()
    assert r.name == "block-test"
    assert r.is_blocking is True  # critical


def test_rule_severity_warning_is_not_blocking() -> None:
    r = _make_rule(severity="warning")
    assert r.is_blocking is False


def test_rule_rejects_empty_name() -> None:
    with pytest.raises(SchemaError, match="name"):
        _make_rule(name="")


def test_rule_rejects_invalid_name() -> None:
    """Uppercase / underscore / dot not allowed."""
    with pytest.raises(SchemaError, match="name"):
        _make_rule(name="Block-Test")
    with pytest.raises(SchemaError, match="name"):
        _make_rule(name="block_test")
    with pytest.raises(SchemaError, match="name"):
        _make_rule(name="block.test")


def test_rule_rejects_unknown_severity() -> None:
    with pytest.raises(SchemaError, match="severity"):
        _make_rule(severity="medium")  # not in VALID_SEVERITIES


def test_rule_rejects_empty_pattern() -> None:
    with pytest.raises(SchemaError, match="pattern"):
        _make_rule(pattern="   ")


def test_rule_rejects_invalid_regex() -> None:
    with pytest.raises(SchemaError, match="regex"):
        _make_rule(pattern=r"[unclosed")


def test_valid_severities_constant() -> None:
    assert frozenset({"critical", "warning", "info"}) == VALID_SEVERITIES


# ── parse / serialize round-trip ────────────────────────────────


def test_parse_markdown_round_trip() -> None:
    original = _make_rule(
        description="Block force push to main branch.",
        message="No force-push to main!",
    )
    text = serialize_markdown(original)
    parsed = parse_markdown(text)
    assert parsed == original


def test_parse_markdown_handles_code_fenced_pattern() -> None:
    """Hookify convention wraps the pattern in code fences."""
    text = """---
name: rule-x
severity: warning
enabled: true
source: user
---
# Description
Test.

# Pattern (regex)
```
\\btest\\b
```

# Message
hi
"""
    rule = parse_markdown(text)
    assert rule.pattern == r"\btest\b"


def test_parse_markdown_disabled_flag() -> None:
    text = """---
name: x
severity: info
enabled: false
source: user
---
# Pattern
foo
"""
    assert parse_markdown(text).enabled is False


def test_parse_markdown_rejects_unclosed_frontmatter() -> None:
    text = "---\nname: x\nthis frontmatter is not closed\n"
    with pytest.raises(SchemaError, match="frontmatter"):
        parse_markdown(text)


def test_parse_markdown_no_frontmatter_falls_back() -> None:
    """Frontmatter-less files parse with default values."""
    text = """# Pattern
\\bsomething\\b

# Message
blocked
"""
    rule = parse_markdown(text)
    assert rule.name == "unnamed"
    assert rule.severity == "critical"
    assert rule.enabled is True
    assert rule.pattern == r"\bsomething\b"


# ── storage ─────────────────────────────────────────────────────


def test_save_and_list_round_trip(tmp_path: Path) -> None:
    rule = _make_rule(name="block-x")
    save_rule(
        rule, license_tier="enterprise", base=tmp_path,
    )
    rules = list_rules(base=tmp_path)
    assert len(rules) == 1
    assert rules[0] == rule


def test_load_rule_missing_returns_none(tmp_path: Path) -> None:
    assert load_rule("nope", base=tmp_path) is None


def test_save_rule_refuses_collision_without_force(tmp_path: Path) -> None:
    rule = _make_rule(name="dup-test")
    save_rule(rule, license_tier="enterprise", base=tmp_path)
    with pytest.raises(RuleError, match="already exists"):
        save_rule(rule, license_tier="enterprise", base=tmp_path)


def test_save_rule_overwrite_with_force(tmp_path: Path) -> None:
    rule = _make_rule(name="dup-test", message="v1")
    save_rule(rule, license_tier="enterprise", base=tmp_path)
    rule2 = replace(rule, message="v2")
    save_rule(
        rule2, overwrite=True, license_tier="enterprise", base=tmp_path,
    )
    loaded = load_rule("dup-test", base=tmp_path)
    assert loaded is not None
    assert loaded.message == "v2"


def test_delete_rule_removes_file(tmp_path: Path) -> None:
    rule = _make_rule(name="to-remove")
    save_rule(rule, license_tier="enterprise", base=tmp_path)
    assert delete_rule("to-remove", base=tmp_path) is True
    assert load_rule("to-remove", base=tmp_path) is None


def test_delete_rule_missing_returns_false(tmp_path: Path) -> None:
    assert delete_rule("does-not-exist", base=tmp_path) is False


def test_set_enabled_toggles(tmp_path: Path) -> None:
    rule = _make_rule(name="toggle-test", enabled=True)
    save_rule(rule, license_tier="enterprise", base=tmp_path)
    assert set_enabled("toggle-test", False, base=tmp_path) is True
    assert load_rule("toggle-test", base=tmp_path).enabled is False  # type: ignore[union-attr]
    assert set_enabled("toggle-test", True, base=tmp_path) is True
    assert load_rule("toggle-test", base=tmp_path).enabled is True  # type: ignore[union-attr]


def test_set_enabled_missing_returns_false(tmp_path: Path) -> None:
    assert set_enabled("nope", True, base=tmp_path) is False


# ── license gate ────────────────────────────────────────────────


def test_solo_free_rule_limit_blocks_fourth(tmp_path: Path) -> None:
    """Solo Free can save 3 user rules. The 4th must error."""
    for i in range(SOLO_FREE_RULE_LIMIT):
        save_rule(
            _make_rule(name=f"rule-{i}"),
            license_tier="free",
            base=tmp_path,
        )
    with pytest.raises(RuleError, match="Solo Free"):
        save_rule(
            _make_rule(name="rule-too-many"),
            license_tier="free",
            base=tmp_path,
        )


def test_pro_tier_unlimited(tmp_path: Path) -> None:
    """Pro+ tiers MUST NOT hit the quota."""
    for i in range(SOLO_FREE_RULE_LIMIT + 5):
        save_rule(
            _make_rule(name=f"rule-{i}"),
            license_tier="pro",
            base=tmp_path,
        )
    assert len(list_rules(base=tmp_path)) == SOLO_FREE_RULE_LIMIT + 5


def test_solo_free_overwrite_existing_doesnt_increase_count(
    tmp_path: Path,
) -> None:
    """A re-save (overwrite) of an existing rule must NOT trip the
    quota — only NEW rule additions count."""
    for i in range(SOLO_FREE_RULE_LIMIT):
        save_rule(
            _make_rule(name=f"rule-{i}"),
            license_tier="free",
            base=tmp_path,
        )
    # Re-save the first rule with overwrite — must succeed
    save_rule(
        _make_rule(name="rule-0", message="updated"),
        overwrite=True,
        license_tier="free",
        base=tmp_path,
    )


# ── env override ────────────────────────────────────────────────


def test_rules_dir_respects_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    custom = tmp_path / "custom-rules"
    monkeypatch.setenv("AEGIS_RULES_DIR", str(custom))
    assert rules_dir() == custom


# ── matcher ─────────────────────────────────────────────────────


def test_matcher_finds_enabled_rule() -> None:
    rule = _make_rule(name="m-1", pattern=r"\bsensitive\b")
    out = evaluate("this is a sensitive value", [rule])
    assert len(out) == 1
    assert out[0].matched_text == "sensitive"


def test_matcher_skips_disabled() -> None:
    rule = _make_rule(name="off-1", enabled=False, pattern=r"\bfoo\b")
    assert evaluate("foo bar", [rule]) == []


def test_matcher_case_insensitive() -> None:
    """The matcher uses re.IGNORECASE — UPPER, Lower, MiXed all match."""
    rule = _make_rule(name="ci-1", pattern=r"\bsecret\b")
    # Word-boundary requires a non-word neighbour. Use spaces around
    # the keyword so \b on both sides has a real boundary.
    out = evaluate("the SECRET is out", [rule])
    assert len(out) == 1


def test_matcher_returns_multiple_matching_rules() -> None:
    r1 = _make_rule(name="r-1", pattern=r"\bfoo\b")
    r2 = _make_rule(name="r-2", pattern=r"\bbar\b")
    out = evaluate("foo and bar", [r1, r2])
    assert len(out) == 2


def test_any_blocking_returns_true_when_any_critical() -> None:
    r_warn = _make_rule(name="w-1", severity="warning", pattern=r"\bx\b")
    r_crit = _make_rule(name="c-1", severity="critical", pattern=r"\bx\b")
    from aegis.rules.matcher import MatchResult
    matches = [
        MatchResult(rule=r_warn, matched_text="x", span=(0, 1)),
        MatchResult(rule=r_crit, matched_text="x", span=(0, 1)),
    ]
    assert any_blocking(matches) is True


def test_any_blocking_returns_false_when_all_non_critical() -> None:
    r_warn = _make_rule(name="w-1", severity="warning", pattern=r"\bx\b")
    from aegis.rules.matcher import MatchResult
    matches = [
        MatchResult(rule=r_warn, matched_text="x", span=(0, 1)),
    ]
    assert any_blocking(matches) is False


# ── nl_to_regex ─────────────────────────────────────────────────


def test_suggest_regex_recognises_force_push() -> None:
    s = suggest_regex("block force push to main")
    assert "force" in s.pattern.lower()
    assert s.sample_matches  # not empty


def test_suggest_regex_recognises_production() -> None:
    s = suggest_regex("block production folder access")
    import re
    assert re.search(s.pattern, "/var/production/db", flags=re.IGNORECASE)


def test_suggest_regex_recognises_credentials() -> None:
    s = suggest_regex("block exposure of credential")
    assert "credential" in s.pattern.lower() or "secret" in s.pattern.lower()


def test_suggest_regex_quoted_literals() -> None:
    """Quoted literals are escaped and stitched into the pattern.

    Use a sentence with NO other matching keywords so the heuristic
    doesn't AND-combine the literal with extra fragments.
    """
    s = suggest_regex("the word 'verywidget' must trigger")
    import re
    assert re.search(s.pattern, "abc verywidget xyz")
    assert not re.search(s.pattern, "abc xyz")


def test_suggest_regex_empty_input() -> None:
    s = suggest_regex("")
    assert s.pattern == r".+"


def test_suggest_regex_unknown_input_uses_fallback() -> None:
    s = suggest_regex("xyzzy plover quux")
    assert s.pattern == r".+"  # forces user to edit


def test_suggest_regex_returns_well_formed_regex() -> None:
    """Every suggestion MUST be a valid Python regex."""
    import re
    samples = [
        "block force push",
        "stop credential leaks",
        "prevent npm publish",
        "deny production deletion",
        "",
        "totally unknown nonsense",
    ]
    for s in samples:
        sug = suggest_regex(s)
        re.compile(sug.pattern)  # raises on invalid


def test_suggest_rule_name_basic() -> None:
    assert suggest_rule_name("block production rm") == "block-production"
    assert suggest_rule_name("stop credential exposure") == "stop-credential"


def test_suggest_rule_name_empty_fallback() -> None:
    assert suggest_rule_name("") == "custom-rule"
    # Unknown nouns → deterministic hash suffix
    name = suggest_rule_name("xyz unknown phrase")
    assert name.startswith("custom-rule-")


def test_suggest_rule_name_deterministic() -> None:
    """Same input → same output (hash-based fallback)."""
    a = suggest_rule_name("xyz unknown")
    b = suggest_rule_name("xyz unknown")
    assert a == b


# ── CLI wiring ──────────────────────────────────────────────────


def test_cli_rule_subcommand_wired() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # type: ignore[import-not-found]

    parser = aegis_cli.build_parser()
    args = parser.parse_args(
        ["rule", "add", "block-x", "--pattern", r"\btest\b"],
    )
    assert args.fn is aegis_cli.cmd_rule
    assert args.rule_action == "add"
    assert args.name == "block-x"
    assert args.pattern == r"\btest\b"


def test_cli_rule_list_no_args() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # type: ignore[import-not-found]

    parser = aegis_cli.build_parser()
    args = parser.parse_args(["rule", "list"])
    assert args.rule_action == "list"


def test_cli_rule_test_requires_text() -> None:
    import contextlib
    import io
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # type: ignore[import-not-found]

    parser = aegis_cli.build_parser()
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        parser.parse_args(["rule", "test"])
