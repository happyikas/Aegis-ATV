"""Tier → feature manifest + the ``has_feature()`` runtime gate.

The contract this module honors (from ``docs/LICENSE_KEY.md`` §4):

* ``has_feature("X")`` returns **True** when:

  - the active license's tier transitively includes ``X`` (per
    :data:`TIER_FEATURES`), AND
  - if the license also declares an explicit ``features`` allow-list,
    ``X`` appears in it.

  The double-check is defense-in-depth against a forged claim that
  inflates ``features`` beyond what the tier permits — the
  most-restrictive of (tier expansion, explicit list) wins.

* For Solo Free (no license file present, or invalid/expired),
  the active tier is ``"free"`` whose feature set is empty, so
  ``has_feature("X")`` returns **False** for every paid feature
  *silently*. We never spam an upsell banner when a Solo Free user
  hits a paid feature path; they opted into Solo Free deliberately.

The helper :func:`require_feature` is for paths that *must* run only
under a paying tier (e.g. dashboards that publish data outside the
laptop). It raises :class:`FeatureUnavailable` with a structured
message; callers can catch it and route to a "upgrade to Pro" UX
instead of crashing.
"""

from __future__ import annotations

from typing import Final

from aegis.license.verify import LicenseClaims

# ──────────────────────────────────────────────────────────────────
# Feature manifest — the public contract paired with PRICING.md.
#
# Adding a new feature: pick the *minimum* tier where it should be
# available, append it to that tier's frozenset. Tiers higher in the
# stack inherit automatically via the ``|`` union below.
# ──────────────────────────────────────────────────────────────────


_FREE_FEATURES: Final[frozenset[str]] = frozenset()

_PRO_FEATURES: Final[frozenset[str]] = frozenset({
    "advisor.full",            # 8-advisor pipeline ON
    "judge.haiku",             # Anthropic Haiku judge for grey-zone
    "judge.phi35",             # local Phi-3.5-mini judge
    "embedding.bge-local",     # local bge embedding model
    "audit.remote-backup",     # encrypted off-laptop backup of audit.jsonl
})

_TEAM_FEATURES: Final[frozenset[str]] = _PRO_FEATURES | frozenset({
    "sidecar.multi-tenant",    # FastAPI multi-tenant deployment
    "atmu.2pc",                # ATMU 2-Phase Commit + compensation plans
    "report.cross-cuts",       # --by-aid / --by-channel / --by-provider /
                               #   --by-aid-and-provider report views
    "slack-connect",           # Slack Connect channel for the team
})

_ENTERPRISE_FEATURES: Final[frozenset[str]] = _TEAM_FEATURES | frozenset({
    "ham.tee-bind",                       # Hardware Attestation Manifest TEE bind
    "audit.aes-gcm-journal",              # AES-GCM encrypted journal
    "audit-patrol",                       # AuditPatrol background re-validation
    "cost-attestation.dual-key",          # Claim 34 dual-key cost-divergence
    "compliance.evidence-packaging",      # SOC 2 / EU AI Act evidence export
})


TIER_FEATURES: Final[dict[str, frozenset[str]]] = {
    "free": _FREE_FEATURES,
    "pro": _PRO_FEATURES,
    "team": _TEAM_FEATURES,
    "enterprise": _ENTERPRISE_FEATURES,
}


def features_for(claims: LicenseClaims | None) -> frozenset[str]:
    """Return the effective feature set for ``claims``.

    Solo Free (``claims is None``) returns an empty set. A license's
    set is the **intersection** of:

      * the tier's transitively-expanded feature set, and
      * the license's explicit ``features`` claim (if non-empty).

    Intersection semantics mean a forged ``features`` claim can only
    *narrow* what the runtime grants, not widen it — the tier
    manifest is authoritative for the upper bound.
    """
    if claims is None:
        return frozenset()

    tier_set = TIER_FEATURES.get(claims.tier, frozenset())
    if not claims.features:
        # No explicit allow-list → just the tier expansion. Common
        # case for license tokens that don't bother enumerating
        # features (the issuer service may omit the list to keep
        # tokens compact).
        return tier_set

    explicit = frozenset(claims.features)
    return tier_set & explicit


# ──────────────────────────────────────────────────────────────────
# Runtime accessor
# ──────────────────────────────────────────────────────────────────


# Module-level cache of the active license's effective feature set.
# Set via :func:`set_active_license`; reset to "Solo Free" via
# :func:`set_active_license(None)`. The cache lives at module scope
# so :func:`has_feature` can be imported and called in tight loops
# without re-parsing JWS on every call.
_active_features: frozenset[str] = frozenset()
_active_claims: LicenseClaims | None = None


def set_active_license(claims: LicenseClaims | None) -> None:
    """Install the verified license claims as the runtime's active
    license. Pass ``None`` to revert to Solo Free.

    Idempotent. Thread-safety: writes to module globals; readers see
    a consistent snapshot because Python's GIL makes individual
    assignments atomic at the bytecode level. If callers mutate this
    from multiple threads concurrently they should serialize via
    their own lock — Aegis itself only sets the license at startup
    or via the CLI's ``aegis license activate`` (single-writer).
    """
    global _active_features, _active_claims
    _active_claims = claims
    _active_features = features_for(claims)


def get_active_claims() -> LicenseClaims | None:
    """Return the currently-active claims, or ``None`` for Solo Free."""
    return _active_claims


def get_active_tier() -> str:
    """Return the active tier name, defaulting to ``"free"``."""
    return _active_claims.tier if _active_claims is not None else "free"


def has_feature(feature: str) -> bool:
    """Return ``True`` if the active license grants ``feature``.

    Solo Free returns ``False`` for every paid feature *silently* —
    no banner, no log line, no upsell. The caller decides whether
    to route to a "upgrade to Pro" UX or just no-op. This is the
    canonical entry point for code paths that branch on tier (e.g.
    advisor pipeline, sidecar mode wiring).
    """
    return feature in _active_features


class FeatureUnavailableError(RuntimeError):
    """Raised by :func:`require_feature` when the active license
    doesn't grant the requested feature.

    Carries the feature name, the active tier, and the minimum tier
    that grants it (when known). The CLI surface catches this and
    renders a structured "upgrade to <tier>" message instead of a
    raw stacktrace.
    """

    def __init__(
        self, feature: str, *, active_tier: str, min_tier: str | None,
    ) -> None:
        self.feature = feature
        self.active_tier = active_tier
        self.min_tier = min_tier
        msg = (
            f"feature {feature!r} requires "
            f"{f'tier {min_tier!r} or higher' if min_tier else 'a paid license'}; "
            f"active tier is {active_tier!r}"
        )
        super().__init__(msg)


def _min_tier_for(feature: str) -> str | None:
    """Walk tiers from cheapest → most expensive; return the first
    one that grants ``feature``. ``None`` if no tier grants it."""
    for tier in ("free", "pro", "team", "enterprise"):
        if feature in TIER_FEATURES.get(tier, frozenset()):
            return tier
    return None


def require_feature(feature: str) -> None:
    """Hard gate. Raises :class:`FeatureUnavailableError` when the
    active license doesn't grant ``feature``. Use this only on paths
    that truly cannot run without the feature (e.g. "publish to
    remote backup" — no graceful degradation possible)."""
    if not has_feature(feature):
        raise FeatureUnavailableError(
            feature,
            active_tier=get_active_tier(),
            min_tier=_min_tier_for(feature),
        )
