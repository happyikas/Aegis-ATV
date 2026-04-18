"""Tests for Step 320 — blast radius lookup."""

from __future__ import annotations

import pytest

from aegis.firewall.core import FirewallContext
from aegis.firewall.step320_blast import TOOL_BLAST_TABLE, UNKNOWN_TOOL_BLAST, run
from tests.unit._firewall_helpers import ZERO_ATV, make_input


@pytest.mark.parametrize("tool, expected", list(TOOL_BLAST_TABLE.items()))
def test_known_tools_get_table_value(tool: str, expected: int) -> None:
    ctx = FirewallContext()
    r = run(ZERO_ATV, make_input(tool_name=tool), ctx)
    assert r.verdict is None
    assert ctx.blast_radius == expected


def test_unknown_tool_defaults_to_medium() -> None:
    ctx = FirewallContext()
    run(ZERO_ATV, make_input(tool_name="some_brand_new_tool"), ctx)
    assert ctx.blast_radius == UNKNOWN_TOOL_BLAST


def test_step_never_blocks() -> None:
    ctx = FirewallContext()
    r = run(ZERO_ATV, make_input(tool_name="transfer_funds"), ctx)
    assert r.verdict is None  # only publishes; never short-circuits
