"""On-disk license storage + activation log.

Two files under ``~/.aegis/``:

* ``license.jwt`` — the compact JWS token. Single line, owner-only
  permissions (``0600``).
* ``license.log`` — append-only newline-delimited JSON. Each line is
  an event: ``activate`` / ``deactivate`` / ``verify-failed``. The
  log is *not* a security boundary (audit chain is the boundary);
  it's an operator convenience for "what happened to my license?"
  forensics.

Default paths can be overridden with the ``AEGIS_LICENSE_PATH`` and
``AEGIS_LICENSE_LOG_PATH`` env vars, primarily for tests.

This module is the only place that mutates license-file state, so
the CLI and the test suite share a single code path.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from aegis.license.features import set_active_license
from aegis.license.verify import (
    LicenseClaims,
    LicenseVerifyError,
    verify_license,
)


def license_path() -> Path:
    """Default ``~/.aegis/license.jwt``, overridable via env."""
    env = os.environ.get("AEGIS_LICENSE_PATH")
    if env:
        return Path(env)
    return Path.home() / ".aegis" / "license.jwt"


def license_log_path() -> Path:
    env = os.environ.get("AEGIS_LICENSE_LOG_PATH")
    if env:
        return Path(env)
    return Path.home() / ".aegis" / "license.log"


def _append_log(event: dict[str, Any]) -> None:
    """Append one JSON line to ``license.log``. Never raises — log
    write failures must not crash license activation. Operator
    convenience surface only; the license itself is already verified
    by the time we get here."""
    with contextlib.suppress(OSError):
        path = license_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": int(time.time()), **event}) + "\n")


def write_license(token: str) -> None:
    """Persist ``token`` to ``license.jwt`` with owner-only perms.

    Caller is responsible for verifying the token *before* calling
    this — :func:`activate_from_path` is the canonical wrapper that
    verifies + persists in one step.
    """
    path = license_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.strip() + "\n", encoding="utf-8")
    # Owner read/write only. License token isn't a secret per se,
    # but tightening perms means a curious reader on a shared box
    # can't grep their colleague's license_id without sudo. Best-
    # effort — Windows / network FS without POSIX perms ignore.
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def read_license() -> str | None:
    """Read the raw JWS from disk. Returns ``None`` when there's no
    license file (the Solo Free state)."""
    path = license_path()
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def remove_license() -> bool:
    """Delete ``license.jwt``. Returns ``True`` if a file was
    removed, ``False`` if there was nothing there."""
    path = license_path()
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def activate_from_path(
    src: Path,
    *,
    local_burnin_id: str | None = None,
) -> LicenseClaims:
    """Verify a license file at ``src`` and, on success, install it
    as the active license.

    Steps:
        1. Read the token from ``src``.
        2. Verify (Ed25519 + claims + optional burn-in bind).
        3. Persist to ``~/.aegis/license.jwt`` with 0600 perms.
        4. Install in the runtime via :func:`set_active_license`.
        5. Append an ``activate`` event to ``license.log``.

    Raises :class:`LicenseVerifyError` on validation failure (the
    file at ``src`` is *not* persisted in that case).
    """
    token = src.read_text(encoding="utf-8").strip()
    try:
        claims = verify_license(token, local_burnin_id=local_burnin_id)
    except LicenseVerifyError as e:
        _append_log({
            "event": "verify-failed",
            "src": str(src),
            "reason": e.reason,
        })
        raise

    write_license(token)
    set_active_license(claims)
    _append_log({
        "event": "activate",
        "tier": claims.tier,
        "license_id": claims.license_id,
        "exp": claims.exp,
        "kid": claims.kid,
    })
    return claims


def deactivate() -> bool:
    """Remove the active license and revert to Solo Free.

    Returns ``True`` if a file was removed; ``False`` when there was
    nothing active. Always reverts the runtime to Solo Free
    regardless (idempotent).
    """
    removed = remove_license()
    set_active_license(None)
    _append_log({"event": "deactivate", "removed": removed})
    return removed


def init_active_from_disk(
    *,
    local_burnin_id: str | None = None,
) -> LicenseClaims | None:
    """Boot-time hook: read ``license.jwt`` if present, verify it,
    and install as the active license.

    Returns the verified claims, or ``None`` if there's no license
    file or verification failed. **Never raises** — boot-time license
    failures degrade to Solo Free silently and are recorded to
    ``license.log`` so :func:`aegis license status` can surface the
    reason later.

    This is the function the runtime startup calls. The CLI's
    ``aegis license activate`` calls :func:`activate_from_path`
    directly.
    """
    token = read_license()
    if token is None:
        set_active_license(None)
        return None
    try:
        claims = verify_license(token, local_burnin_id=local_burnin_id)
    except LicenseVerifyError as e:
        _append_log({
            "event": "verify-failed",
            "src": str(license_path()),
            "reason": e.reason,
        })
        set_active_license(None)
        return None

    set_active_license(claims)
    return claims
