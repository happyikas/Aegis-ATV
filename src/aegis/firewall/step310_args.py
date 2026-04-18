"""Step 310 — Argument Inspection (PLAN 6.4)."""

from __future__ import annotations

import re

import numpy as np

from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

DANGEROUS_PATTERNS: list[str] = [
    r"\brm\s+-rf\s+/",
    r"DROP\s+TABLE",
    r"/etc/(shadow|passwd)",
    r"\bsudo\s+",
    r"\b(exec|system)\s*\(",
]

INJECTION_THRESHOLD = 0.7

_COMPILED = [re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS]


def run(atv: np.ndarray, inp: ATVInput, ctx: FirewallContext) -> StepResult:
    payload = inp.tool_args_json or ""
    for pat, compiled in zip(DANGEROUS_PATTERNS, _COMPILED, strict=True):
        if compiled.search(payload):
            return StepResult(
                "BLOCK",
                f"dangerous pattern: {pat}",
                f"step310: static pattern hit ({pat})",
            )

    inj = float(inp.safety_flags.get("prompt_injection", 0.0))
    if inj > INJECTION_THRESHOLD:
        return StepResult(
            "BLOCK",
            f"prompt injection score {inj:.2f} > {INJECTION_THRESHOLD}",
            f"step310: safety score breach (inj={inj:.2f})",
        )

    return StepResult(None, "", f"step310: ok (inj={inj:.2f})")
