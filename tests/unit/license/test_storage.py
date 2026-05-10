"""Storage tests: write / read / activate / deactivate / boot init."""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from aegis.license import (
    LicenseVerifyError,
    activate_from_path,
    deactivate,
    get_active_tier,
    has_feature,
    init_active_from_disk,
    license_log_path,
    license_path,
    read_license,
    remove_license,
    write_license,
)

# ── path overrides via env ──────────────────────────────────────


def test_default_path_under_home() -> None:
    """Without env override, path points at ~/.aegis/license.jwt."""
    p = license_path()
    assert p.name == "license.jwt"
    assert p.parent.name == ".aegis"


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(custom))
    assert license_path() == custom


def test_log_path_default_under_home() -> None:
    p = license_log_path()
    assert p.name == "license.log"
    assert p.parent.name == ".aegis"


def test_log_path_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "lic.log"))
    assert license_log_path() == tmp_path / "lic.log"


# ── write / read / remove ───────────────────────────────────────


def test_write_then_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "license.jwt"))
    write_license("a.b.c")
    assert read_license() == "a.b.c"


def test_read_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "absent.jwt"))
    assert read_license() is None


def test_write_strips_whitespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "license.jwt"))
    write_license("  trimmed.token.value  \n")
    assert read_license() == "trimmed.token.value"


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX permission bits don't apply on Windows",
)
def test_write_sets_owner_only_perms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    p = tmp_path / "license.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(p))
    write_license("x.y.z")
    mode = p.stat().st_mode & 0o777
    # Exactly 0600 — no group / world bits.
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_remove_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    p = tmp_path / "license.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(p))
    write_license("x.y.z")
    assert remove_license() is True
    assert not p.exists()


def test_remove_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "absent.jwt"))
    assert remove_license() is False


# ── activate_from_path ──────────────────────────────────────────


def test_activate_happy_path_persists_and_activates(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "license.jwt"))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    src = tmp_path / "incoming.jwt"
    src.write_text(mint(tier="pro"))

    claims = activate_from_path(src)
    assert claims.tier == "pro"
    # Persisted to ~/.aegis/license.jwt:
    assert read_license() is not None
    # Active runtime now in pro tier:
    assert get_active_tier() == "pro"
    assert has_feature("advisor.full")
    # Log line written:
    log = (tmp_path / "license.log").read_text().splitlines()
    assert any("activate" in line for line in log)


def test_activate_failure_does_not_persist(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    """Tampered key file → fails verification → degrades to Solo Free
    silently (logged, not thrown — except activate raises so the CLI
    can show the error). The persisted file is NOT updated."""
    target = tmp_path / "license.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(target))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))

    # Tamper the payload after signing.
    token = mint(tier="pro")
    parts = token.split(".")
    bad = f"{parts[0]}.{parts[1]}aaaa.{parts[2]}"  # invalidate sig
    src = tmp_path / "tampered.jwt"
    src.write_text(bad)

    with pytest.raises(LicenseVerifyError):
        activate_from_path(src)

    assert not target.exists()    # NOT persisted
    assert get_active_tier() == "free"
    # Log captured the failure event.
    log = (tmp_path / "license.log").read_text().splitlines()
    assert any("verify-failed" in line for line in log)


# ── deactivate ──────────────────────────────────────────────────


def test_deactivate_removes_file_and_reverts_runtime(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "license.jwt"))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    src = tmp_path / "incoming.jwt"
    src.write_text(mint(tier="enterprise"))

    activate_from_path(src)
    assert has_feature("ham.tee-bind")

    removed = deactivate()
    assert removed is True
    assert not (tmp_path / "license.jwt").exists()
    assert get_active_tier() == "free"
    assert not has_feature("ham.tee-bind")


def test_deactivate_idempotent_when_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "license.jwt"))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    # No file → returns False but doesn't raise; runtime stays Solo Free.
    assert deactivate() is False
    assert get_active_tier() == "free"


# ── init_active_from_disk ───────────────────────────────────────


def test_init_no_file_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "absent.jwt"))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    assert init_active_from_disk() is None
    assert get_active_tier() == "free"


def test_init_valid_file_installs(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    target = tmp_path / "license.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(target))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    target.write_text(mint(tier="team"))

    claims = init_active_from_disk()
    assert claims is not None
    assert claims.tier == "team"
    assert has_feature("sidecar.multi-tenant")


def test_init_invalid_file_degrades_silently(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid file at boot → degrade to Solo Free without raising,
    log the reason. This is the *boot-time* contract that's
    different from `activate_from_path`'s raise-loud contract."""
    target = tmp_path / "license.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(target))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))

    # Tamper the signature.
    parts = mint(tier="pro").split(".")
    target.write_text(f"{parts[0]}.{parts[1]}.AAAA")

    claims = init_active_from_disk()
    assert claims is None
    assert get_active_tier() == "free"
    out = capsys.readouterr()
    assert out.out == ""    # silent
    assert out.err == ""
    # Log captured the failure.
    log = (tmp_path / "license.log").read_text().splitlines()
    assert any(json.loads(line)["event"] == "verify-failed" for line in log)


def test_log_write_failure_is_silent(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    """Pointing the log at an unwritable path doesn't crash the
    activation; the log is operator-convenience only, not a security
    boundary."""
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "license.jwt"))
    # Point log at something that's a *file* (not a directory) so any
    # mkdir on its parent fails — but we want write to fail. Instead
    # use /proc/self/cmdline (read-only) on linux, /etc/hostname on
    # mac, or just a directory we then chmod 0500. Simplest portable
    # trick: point log path *inside* a path where the parent doesn't
    # exist and CAN'T be created (use a file as a "directory").
    blocking_file = tmp_path / "not-a-dir"
    blocking_file.write_text("regular file, not a directory")
    monkeypatch.setenv(
        "AEGIS_LICENSE_LOG_PATH",
        str(blocking_file / "license.log"),
    )

    src = tmp_path / "incoming.jwt"
    src.write_text(mint(tier="pro"))

    # Activation succeeds even though log write would fail (silent).
    claims = activate_from_path(src)
    assert claims.tier == "pro"
