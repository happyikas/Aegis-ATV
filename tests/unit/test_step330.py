"""Tests for Step 330 — human-approval threshold."""

from __future__ import annotations

from aegis.firewall.core import FirewallContext
from aegis.firewall.step330_human import HIGH_BLAST_THRESHOLD, run
from tests.unit._firewall_helpers import ZERO_ATV, make_input


def test_high_blast_requires_approval() -> None:
    ctx = FirewallContext(blast_radius=10)
    r = run(ZERO_ATV, make_input(), ctx)
    assert r.verdict == "REQUIRE_APPROVAL"
    assert "10" in r.reason


def test_threshold_boundary_requires_approval() -> None:
    ctx = FirewallContext(blast_radius=HIGH_BLAST_THRESHOLD)
    r = run(ZERO_ATV, make_input(), ctx)
    assert r.verdict == "REQUIRE_APPROVAL"


def test_below_threshold_passes() -> None:
    ctx = FirewallContext(blast_radius=HIGH_BLAST_THRESHOLD - 1)
    r = run(ZERO_ATV, make_input(), ctx)
    assert r.verdict is None


def test_missing_blast_falls_back_to_medium_and_passes() -> None:
    ctx = FirewallContext()  # blast_radius unset
    r = run(ZERO_ATV, make_input(), ctx)
    assert r.verdict is None
