"""CLI tests for `aegis license <action>`.

Exercise the parser + dispatcher; the underlying logic is covered
by test_storage.py. These tests verify the user-facing surface
(stdout / stderr / exit codes / argparse wiring).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from aegis.license import (
    activate_from_path,
)
from tools import aegis_cli

# ── parser smoke ────────────────────────────────────────────────


def test_parser_license_status() -> None:
    p = aegis_cli.build_parser()
    ns = p.parse_args(["license", "status"])
    assert ns.action == "status"


def test_parser_license_activate_requires_path() -> None:
    p = aegis_cli.build_parser()
    ns = p.parse_args(["license", "activate", "/some/file.jwt"])
    assert ns.action == "activate"
    assert ns.path == "/some/file.jwt"


def test_parser_license_verify_requires_path() -> None:
    p = aegis_cli.build_parser()
    ns = p.parse_args(["license", "verify", "/x.jwt"])
    assert ns.action == "verify"
    assert ns.path == "/x.jwt"


def test_parser_license_unknown_action_fails() -> None:
    p = aegis_cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["license", "definitely-not-a-real-subcommand"])


# ── status ──────────────────────────────────────────────────────


def test_status_solo_free_when_no_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "absent.jwt"))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    p = aegis_cli.build_parser()
    rc = aegis_cli.cmd_license(p.parse_args(["license", "status"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Solo Free" in out
    assert "absent" in out


def test_status_active_license_shows_tier(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "license.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(target))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    target.write_text(mint(tier="enterprise", license_id="lic_E_999"))

    p = aegis_cli.build_parser()
    rc = aegis_cli.cmd_license(p.parse_args(["license", "status"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "tier:" in out
    assert "enterprise" in out
    assert "lic_E_999" in out


def test_status_invalid_file_shows_hint(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "license.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(target))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    parts = mint().split(".")
    target.write_text(f"{parts[0]}.{parts[1]}.AAAA")  # tampered sig

    p = aegis_cli.build_parser()
    rc = aegis_cli.cmd_license(p.parse_args(["license", "status"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Solo Free" in out
    assert "exists but invalid" in out


# ── activate ────────────────────────────────────────────────────


def test_activate_happy_path(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "license.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(target))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    src = tmp_path / "in.jwt"
    src.write_text(mint(tier="pro"))

    p = aegis_cli.build_parser()
    rc = aegis_cli.cmd_license(p.parse_args(["license", "activate", str(src)]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "activated" in out
    assert "tier=pro" in out


def test_activate_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "license.jwt"))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    p = aegis_cli.build_parser()
    rc = aegis_cli.cmd_license(p.parse_args(
        ["license", "activate", "/totally/missing.jwt"],
    ))
    assert rc == 1
    err = capsys.readouterr().err
    assert "file not found" in err


def test_activate_verify_failure(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "license.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(target))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    parts = mint().split(".")
    src = tmp_path / "bad.jwt"
    src.write_text(f"{parts[0]}.{parts[1]}.AAAA")

    p = aegis_cli.build_parser()
    rc = aegis_cli.cmd_license(p.parse_args(["license", "activate", str(src)]))
    assert rc == 1
    err = capsys.readouterr().err
    assert "activate failed" in err
    assert not target.exists()  # NOT installed


# ── verify (no activation) ──────────────────────────────────────


def test_verify_happy_path(
    mint: Callable[..., str],
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = tmp_path / "in.jwt"
    src.write_text(mint(tier="team"))
    p = aegis_cli.build_parser()
    rc = aegis_cli.cmd_license(p.parse_args(["license", "verify", str(src)]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "verify OK" in out
    assert "tier=team" in out


def test_verify_does_not_persist(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    """`verify` must NOT touch ~/.aegis/license.jwt. It's a dry-run
    surface (CI / preview); only `activate` writes to disk."""
    target = tmp_path / "license.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(target))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    src = tmp_path / "in.jwt"
    src.write_text(mint(tier="pro"))
    p = aegis_cli.build_parser()
    aegis_cli.cmd_license(p.parse_args(["license", "verify", str(src)]))
    assert not target.exists()


def test_verify_failure_exits_1(
    mint: Callable[..., str],
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parts = mint().split(".")
    src = tmp_path / "bad.jwt"
    src.write_text(f"{parts[0]}.{parts[1]}.AAAA")
    p = aegis_cli.build_parser()
    rc = aegis_cli.cmd_license(p.parse_args(["license", "verify", str(src)]))
    assert rc == 1
    out = capsys.readouterr().out
    assert "verify failed" in out
    assert "bad-signature" in out


# ── deactivate ──────────────────────────────────────────────────


def test_deactivate_when_active(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "license.jwt"
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(target))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    src = tmp_path / "in.jwt"
    src.write_text(mint(tier="pro"))
    activate_from_path(src)
    assert target.exists()

    p = aegis_cli.build_parser()
    rc = aegis_cli.cmd_license(p.parse_args(["license", "deactivate"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "removed" in out
    assert not target.exists()


def test_deactivate_when_solo_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "license.jwt"))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    p = aegis_cli.build_parser()
    rc = aegis_cli.cmd_license(p.parse_args(["license", "deactivate"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no license to remove" in out


# ── refresh (placeholder) ───────────────────────────────────────


def test_refresh_prints_explanatory_stub_no_outbound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default install must make 0 outbound network requests. The
    refresh command should NOT do anything network-related until the
    issuer service ships."""
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(tmp_path / "license.jwt"))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))
    p = aegis_cli.build_parser()
    rc = aegis_cli.cmd_license(p.parse_args(["license", "refresh"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "not yet active" in out
    assert "Solo Free contract still holds" in out


# ── unknown action fallback (defense in depth) ─────────────────


def test_dispatch_unknown_action_returns_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If somehow the dispatcher is called with an unknown action
    (bypassing argparse), it returns 1 instead of crashing."""
    import argparse
    rc = aegis_cli.cmd_license(argparse.Namespace(action="bogus"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown action" in err
