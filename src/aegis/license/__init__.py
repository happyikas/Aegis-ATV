"""Aegis license-key validation surface.

Public contract used by the rest of the runtime:

* :func:`init_active_from_disk` — call once at startup.
* :func:`has_feature` / :func:`require_feature` — gate paid paths.
* :func:`get_active_claims` / :func:`get_active_tier` — for `aegis
  license status` and forensic surfaces.

The CLI surface (`aegis license activate / status / deactivate /
verify / refresh`) lives in :mod:`tools.aegis_cli`.

This module is **off by default**: a fresh install with no
``~/.aegis/license.jwt`` file is in the Solo Free tier, ``has_feature``
returns ``False`` for every paid feature, and no upsell banner ever
fires. The runtime treats Solo Free as the contract — see
``PRICING.md`` and ``docs/LICENSE_KEY.md`` for the design.
"""

from aegis.license.features import (
    TIER_FEATURES,
    FeatureUnavailableError,
    features_for,
    get_active_claims,
    get_active_tier,
    has_feature,
    require_feature,
    set_active_license,
)
from aegis.license.keys import (
    DEFAULT_KID,
    ISSUER_PUBLIC_KEYS,
    get_issuer_public_key,
)
from aegis.license.storage import (
    activate_from_path,
    deactivate,
    init_active_from_disk,
    license_log_path,
    license_path,
    read_license,
    remove_license,
    write_license,
)
from aegis.license.verify import (
    EXPECTED_AUDIENCE,
    KNOWN_TIERS,
    LicenseClaims,
    LicenseVerifyError,
    verify_license,
)

__all__ = [
    "DEFAULT_KID",
    "EXPECTED_AUDIENCE",
    "ISSUER_PUBLIC_KEYS",
    "KNOWN_TIERS",
    "FeatureUnavailableError",
    "LicenseClaims",
    "LicenseVerifyError",
    "TIER_FEATURES",
    "activate_from_path",
    "deactivate",
    "features_for",
    "get_active_claims",
    "get_active_tier",
    "get_issuer_public_key",
    "has_feature",
    "init_active_from_disk",
    "license_log_path",
    "license_path",
    "read_license",
    "remove_license",
    "require_feature",
    "set_active_license",
    "verify_license",
    "write_license",
]
