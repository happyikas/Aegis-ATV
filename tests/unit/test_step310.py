"""Tests for Step 310 — argument inspection."""

from __future__ import annotations

import pytest

from aegis.firewall.core import FirewallContext
from aegis.firewall.step310_args import run
from tests.unit._firewall_helpers import ZERO_ATV, make_input


@pytest.mark.parametrize(
    "args",
    [
        '{"cmd":"rm -rf /"}',
        '{"sql":"DROP TABLE users"}',
        '{"path":"/etc/shadow"}',
        '{"cmd":"sudo cat /var/log/secure"}',
        '{"code":"exec(\\"import os\\")"}',
    ],
)
def test_blocks_dangerous_patterns(args: str) -> None:
    inp = make_input(tool_args_json=args)
    r = run(ZERO_ATV, inp, FirewallContext())
    assert r.verdict == "BLOCK"


def test_blocks_high_injection_score() -> None:
    inp = make_input(safety_flags={"prompt_injection": 0.95})
    r = run(ZERO_ATV, inp, FirewallContext())
    assert r.verdict == "BLOCK"
    assert "prompt injection" in r.reason


def test_passes_clean_args() -> None:
    inp = make_input(tool_args_json='{"path":"./data/report.txt"}')
    r = run(ZERO_ATV, inp, FirewallContext())
    assert r.verdict is None


def test_passes_low_injection_score() -> None:
    inp = make_input(safety_flags={"prompt_injection": 0.05})
    r = run(ZERO_ATV, inp, FirewallContext())
    assert r.verdict is None
