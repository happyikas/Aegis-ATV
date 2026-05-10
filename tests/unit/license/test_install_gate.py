"""License gate tests for the install / advisor surface.

Covers steps 5-7 of ``docs/LICENSE_KEY.md §9``:

* Step 5 — ``aegis install --mode local --profile pro/cloud`` refused
  without a license that grants ``advisor.full``.
* Step 6 — runtime advisor pipeline (``_compute_action_advice``) falls
  back to ``None`` silently when ``advisor.full`` is not granted.
* Step 7 — ``aegis install --mode sidecar`` refused without a license
  that grants ``sidecar.multi-tenant``.

Each test isolates state via the ``reset_active_license`` fixture so
no leftover license bleeds across the suite.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest


def _install_license_to_disk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    token: str,
) -> None:
    """Write a JWS to a tmp ``license.jwt`` and point
    ``AEGIS_LICENSE_PATH`` at it. The install gate's
    ``init_active_from_disk()`` call will then pick it up. Use this
    instead of ``set_active_license`` directly because the gate
    intentionally reloads from disk (so a freshly-activated key is
    picked up without restarting the shell)."""
    license_path = tmp_path / "license.jwt"
    license_path.write_text(token)
    monkeypatch.setenv("AEGIS_LICENSE_PATH", str(license_path))
    monkeypatch.setenv("AEGIS_LICENSE_LOG_PATH", str(tmp_path / "license.log"))


# ── install gate ─────────────────────────────────────────────────


def test_install_gate_allows_free_local(
    reset_active_license: None,
) -> None:
    from tools.aegis_cli import _install_license_gate_ok
    assert _install_license_gate_ok(mode="local", profile="free") is True


def test_install_gate_refuses_pro_without_license(
    capsys: pytest.CaptureFixture[str],
    reset_active_license: None,
) -> None:
    from tools.aegis_cli import _install_license_gate_ok
    ok = _install_license_gate_ok(mode="local", profile="pro")
    assert ok is False
    err = capsys.readouterr().err
    assert "--profile pro requires" in err
    assert "Pro" in err and "Team" in err and "Enterprise" in err
    # The user is pointed at the two remediation paths.
    assert "aegis license activate" in err
    assert "--profile free" in err


def test_install_gate_refuses_cloud_without_license(
    capsys: pytest.CaptureFixture[str],
    reset_active_license: None,
) -> None:
    from tools.aegis_cli import _install_license_gate_ok
    ok = _install_license_gate_ok(mode="local", profile="cloud")
    assert ok is False
    err = capsys.readouterr().err
    assert "--profile cloud requires" in err


def test_install_gate_allows_pro_with_pro_license(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    from tools.aegis_cli import _install_license_gate_ok
    _install_license_to_disk(monkeypatch, tmp_path, token=mint(tier="pro"))
    assert _install_license_gate_ok(mode="local", profile="pro") is True


def test_install_gate_allows_cloud_with_pro_license(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    """Cloud profile uses Pro's advisor.full marker; a Pro license
    is sufficient. Cloud's *additional* features (judge.haiku) are
    gated separately at runtime when the haiku judge is selected."""
    from tools.aegis_cli import _install_license_gate_ok
    _install_license_to_disk(monkeypatch, tmp_path, token=mint(tier="pro"))
    assert _install_license_gate_ok(mode="local", profile="cloud") is True


def test_install_gate_refuses_sidecar_without_license(
    capsys: pytest.CaptureFixture[str],
    reset_active_license: None,
) -> None:
    from tools.aegis_cli import _install_license_gate_ok
    ok = _install_license_gate_ok(mode="sidecar", profile="free")
    assert ok is False
    err = capsys.readouterr().err
    assert "--mode sidecar requires" in err
    assert "Team" in err and "Enterprise" in err
    assert "aegis license activate" in err
    # User pointed at the local-mode alternative.
    assert "--mode local" in err


def test_install_gate_refuses_sidecar_with_pro_license(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    reset_active_license: None,
) -> None:
    """Sidecar requires Team+, not Pro."""
    from tools.aegis_cli import _install_license_gate_ok
    _install_license_to_disk(monkeypatch, tmp_path, token=mint(tier="pro"))
    ok = _install_license_gate_ok(mode="sidecar", profile="free")
    assert ok is False
    err = capsys.readouterr().err
    assert "Active tier: pro" in err


def test_install_gate_allows_sidecar_with_team_license(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    from tools.aegis_cli import _install_license_gate_ok
    _install_license_to_disk(monkeypatch, tmp_path, token=mint(tier="team"))
    assert _install_license_gate_ok(mode="sidecar", profile="free") is True


def test_install_gate_allows_sidecar_with_enterprise_license(
    mint: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_active_license: None,
) -> None:
    from tools.aegis_cli import _install_license_gate_ok
    _install_license_to_disk(monkeypatch, tmp_path, token=mint(tier="enterprise"))
    assert _install_license_gate_ok(mode="sidecar", profile="free") is True


def test_install_gate_message_names_active_tier(
    mint: Callable[..., str],
    capsys: pytest.CaptureFixture[str],
    reset_active_license: None,
) -> None:
    """The error message tells the user which tier they're on so they
    don't have to run `aegis license status` to figure it out."""
    from tools.aegis_cli import _install_license_gate_ok
    # Solo Free → "Active tier: free"
    _install_license_gate_ok(mode="local", profile="pro")
    err = capsys.readouterr().err
    assert "Active tier: free" in err


def test_install_gate_defensive_on_broken_license_module(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reset_active_license: None,
) -> None:
    """If `aegis.license` cannot be imported or raises, the gate
    treats the install as Solo Free — refusing the upgrade with the
    standard message rather than crashing or silently allowing."""
    import sys as _sys

    # Sabotage init_active_from_disk by patching the symbol in
    # tools.aegis_cli's import namespace. Easiest: pre-poison the
    # module's local import with an attribute error.
    import aegis.license as license_mod

    def boom() -> None:
        raise RuntimeError("simulated license module failure")

    monkeypatch.setattr(license_mod, "init_active_from_disk", boom)
    # Drop tools.aegis_cli from cache so its closure re-imports under
    # the patch.
    _sys.modules.pop("tools.aegis_cli", None)

    from tools.aegis_cli import _install_license_gate_ok
    ok = _install_license_gate_ok(mode="local", profile="pro")
    assert ok is False
    # Defaults to "free" active tier in the error message.
    err = capsys.readouterr().err
    assert "Active tier: free" in err


# ── runtime advisor gate ─────────────────────────────────────────


def test_advisor_gate_returns_none_under_solo_free(
    monkeypatch: pytest.MonkeyPatch,
    reset_active_license: None,
) -> None:
    """Even with AEGIS_ADVISOR_ENABLED=1, the advisor falls back to
    None when no license grants advisor.full. Same observable
    behavior as the advisor being off."""
    import importlib

    monkeypatch.setenv("AEGIS_ADVISOR_ENABLED", "1")
    # Force re-import so the module-level ADVISOR_ENABLED picks up.
    import tools.aegis_local_hook as hook_mod
    importlib.reload(hook_mod)

    # Solo Free — no license active.
    result = hook_mod._compute_action_advice(
        inp=_fake_inp(),
        verdict=_fake_verdict(),
        tool_name="Bash",
        transcript_path=None,
        explain_block={},
    )
    assert result is None


def test_advisor_gate_silent_no_banner_under_solo_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reset_active_license: None,
) -> None:
    """The Solo Free contract: gate falls through SILENTLY. No print,
    no log line, no banner. Same as PR #157's has_feature contract."""
    import importlib
    monkeypatch.setenv("AEGIS_ADVISOR_ENABLED", "1")
    import tools.aegis_local_hook as hook_mod
    importlib.reload(hook_mod)
    hook_mod._compute_action_advice(
        inp=_fake_inp(),
        verdict=_fake_verdict(),
        tool_name="Bash",
        transcript_path=None,
        explain_block={},
    )
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


def test_advisor_gate_returns_none_when_advisor_disabled(
    monkeypatch: pytest.MonkeyPatch,
    reset_active_license: None,
) -> None:
    """Pre-existing check: when AEGIS_ADVISOR_ENABLED=0 the gate
    bails before even reaching the license check. Unchanged by this
    PR; smoke-tested here to confirm the new license check didn't
    accidentally tighten the disabled-path semantics."""
    import importlib
    monkeypatch.setenv("AEGIS_ADVISOR_ENABLED", "0")
    import tools.aegis_local_hook as hook_mod
    importlib.reload(hook_mod)
    result = hook_mod._compute_action_advice(
        inp=_fake_inp(),
        verdict=_fake_verdict(),
        tool_name="Bash",
        transcript_path=None,
        explain_block={},
    )
    assert result is None


# ── test helpers ────────────────────────────────────────────────


class _FakeHeader:
    aid = "soak-test-aid"


class _FakeInp:
    header = _FakeHeader()


class _FakeVerdict:
    decision = "ALLOW"
    reason = ""
    step_traces: dict[str, str] = {}


def _fake_inp() -> _FakeInp:
    return _FakeInp()


def _fake_verdict() -> _FakeVerdict:
    return _FakeVerdict()
