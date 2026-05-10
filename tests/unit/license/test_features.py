"""Feature-manifest + has_feature() / require_feature() tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from aegis.license import (
    TIER_FEATURES,
    FeatureUnavailableError,
    features_for,
    get_active_claims,
    get_active_tier,
    has_feature,
    require_feature,
    set_active_license,
    verify_license,
)

# ── tier inheritance ────────────────────────────────────────────


def test_pro_extends_free() -> None:
    for f in TIER_FEATURES["free"]:
        assert f in TIER_FEATURES["pro"]


def test_team_extends_pro() -> None:
    for f in TIER_FEATURES["pro"]:
        assert f in TIER_FEATURES["team"]


def test_enterprise_extends_team() -> None:
    for f in TIER_FEATURES["team"]:
        assert f in TIER_FEATURES["enterprise"]


def test_specific_features_at_each_tier() -> None:
    """Sanity-check the public contract that the docs commit to."""
    assert "advisor.full" in TIER_FEATURES["pro"]
    assert "advisor.full" not in TIER_FEATURES["free"]
    assert "sidecar.multi-tenant" in TIER_FEATURES["team"]
    assert "sidecar.multi-tenant" not in TIER_FEATURES["pro"]
    assert "ham.tee-bind" in TIER_FEATURES["enterprise"]
    assert "ham.tee-bind" not in TIER_FEATURES["team"]


# ── features_for() ──────────────────────────────────────────────


def test_features_for_none_is_empty() -> None:
    assert features_for(None) == frozenset()


def test_features_for_pro_has_pro_set(
    mint: Callable[..., str],
) -> None:
    claims = verify_license(mint(tier="pro"))
    assert features_for(claims) == TIER_FEATURES["pro"]


def test_features_for_explicit_intersects_with_tier(
    mint: Callable[..., str],
) -> None:
    """An explicit features list narrows the tier's set; a forged
    extra feature is dropped."""
    claims = verify_license(mint(
        tier="pro",
        features=["advisor.full", "judge.haiku", "ham.tee-bind"],
    ))
    fset = features_for(claims)
    # advisor.full + judge.haiku are in pro → keep them.
    assert "advisor.full" in fset
    assert "judge.haiku" in fset
    # ham.tee-bind is enterprise-only — defense in depth means even
    # though the license tries to claim it, it's NOT granted.
    assert "ham.tee-bind" not in fset


def test_features_for_unknown_tier_returns_empty(
    mint: Callable[..., str],
) -> None:
    """If somehow an unknown tier slips through (defense in depth),
    grant nothing rather than crashing."""
    # Bypass verify by hand-crafting a claims tuple with a bogus tier.
    from aegis.license.verify import LicenseClaims
    bad = LicenseClaims(
        tier="ultraviolet", iss="i", sub="s", aud="aegis-mvp",
        iat=0, exp=10**12, license_id="x", seats=1,
        features=("advisor.full",), burnin_bind=None, kid="k",
    )
    assert features_for(bad) == frozenset()


# ── runtime accessor ────────────────────────────────────────────


def test_solo_free_has_no_features(reset_active_license: None) -> None:
    assert get_active_claims() is None
    assert get_active_tier() == "free"
    assert not has_feature("advisor.full")
    assert not has_feature("anything")


def test_set_active_license_pro_grants_pro_features(
    mint: Callable[..., str], reset_active_license: None,
) -> None:
    claims = verify_license(mint(tier="pro"))
    set_active_license(claims)
    assert get_active_tier() == "pro"
    assert has_feature("advisor.full")
    assert has_feature("judge.haiku")
    # Team-only stays False.
    assert not has_feature("sidecar.multi-tenant")


def test_set_active_license_none_reverts(
    mint: Callable[..., str], reset_active_license: None,
) -> None:
    claims = verify_license(mint(tier="enterprise"))
    set_active_license(claims)
    assert has_feature("ham.tee-bind")
    set_active_license(None)
    assert not has_feature("ham.tee-bind")
    assert get_active_tier() == "free"


# ── require_feature() ──────────────────────────────────────────


def test_require_feature_raises_under_solo_free(
    reset_active_license: None,
) -> None:
    with pytest.raises(FeatureUnavailableError) as exc:
        require_feature("advisor.full")
    assert exc.value.feature == "advisor.full"
    assert exc.value.active_tier == "free"
    assert exc.value.min_tier == "pro"


def test_require_feature_passes_when_active(
    mint: Callable[..., str], reset_active_license: None,
) -> None:
    set_active_license(verify_license(mint(tier="pro")))
    require_feature("advisor.full")  # no exception


def test_require_feature_unknown_feature(
    reset_active_license: None,
) -> None:
    """A feature name that no tier grants → min_tier=None and the
    error message says 'a paid license' rather than 'tier X'."""
    with pytest.raises(FeatureUnavailableError) as exc:
        require_feature("not-a-real-feature")
    assert exc.value.min_tier is None
    assert "paid license" in str(exc.value)


def test_silent_no_banner_under_solo_free(
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The Solo Free contract: ``has_feature`` for a paid feature
    returns False *silently* — no print, no log line."""
    assert not has_feature("advisor.full")
    assert not has_feature("ham.tee-bind")
    assert not has_feature("sidecar.multi-tenant")
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""
