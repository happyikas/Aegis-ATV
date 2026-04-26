"""Step 311 — Donor pattern rule pack (D11, partial).

Ports the 7 stdlib-only pattern rules from
``_donor/aegis-mvp/atmu/rules/`` that close the eight gap incidents
surfaced by the Phase 3 ``test_donor_incidents_e2e`` matrix:

    persona_drift     I-01  → REQUIRE_APPROVAL
    exfil_url         I-04  → BLOCK
    sandbox_escape    I-06  → BLOCK
    exfil_url (DNS)   I-07  → BLOCK  (same rule, different TLD)
    prompt_injection  I-08  → REQUIRE_APPROVAL
    mcp_injection     I-09  → BLOCK
    git_destructive   I-10  → BLOCK
    payment_overflow  I-11  → BLOCK

Two donor rules are intentionally NOT included here because they need
runtime context that is not yet ported under Phase 2:

* ``cost_overflow``      — depends on D10 ``cost.budget`` + per-day
  spend tracker (P1 priority).
* ``malfunction_pattern`` — depends on D7 ``monitor.malfunction``
  (P1 priority).

Step 311 runs immediately after step310 (the regex/sensitive-path
gate) and before step312 (normalisation). The donor rules each scan
either the canonical ``tool`` name + ``tool_args_json`` text (for the
regex-based rules) or the parsed JSON object (for ``payment_overflow``
which checks numeric ceilings).

All seven rules are self-contained: stdlib only, deterministic, no
external service calls, no DB lookups.
"""

from __future__ import annotations

import json
import re
from typing import Any

import numpy as np

from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

# ─────────────────────────────────────────────────────────────────────
# Tool sets (donor: aegis-mvp/atmu/rules/_tools.py)
# ─────────────────────────────────────────────────────────────────────
_SHELL_TOOLS: frozenset[str] = frozenset({
    "Bash", "shell", "bash", "exec", "sh", "zsh", "fish",
    "execute_shell", "run_command", "terminal",
})
_GIT_TOOLS: frozenset[str] = _SHELL_TOOLS | {"git"}

_EXFIL_TOOLS: frozenset[str] = frozenset({
    "render_image", "fetch", "http_request", "curl", "browse",
    "embed_image", "send_email", "post_message",
})

_PROMPT_INJECTION_TOOLS: frozenset[str] = frozenset({
    "fetch", "read_file", "search", "rag_query", "browse", "read_page",
})

_MCP_REGISTRATION_TOOLS: frozenset[str] = frozenset({
    "register_mcp_tool", "load_mcp", "mcp_describe", "mcp_register",
})

# ─────────────────────────────────────────────────────────────────────
# Compiled patterns (donor: rules/*.py — verbatim where possible)
# ─────────────────────────────────────────────────────────────────────
_PERSONA_DRIFT = re.compile(
    r"\b(?:"
    r"repeat (?:your|the) (?:system|initial|original) (?:prompt|instructions)"
    r"|what (?:are|were) your instructions"
    r"|reveal (?:your|the) (?:system|hidden) prompt"
    r"|show me (?:your|the) (?:system|original) message"
    r")",
    re.IGNORECASE,
)

_EXFIL_URL = re.compile(
    r'https?://[^\s"\']*?'
    r"(?:[A-Za-z0-9+/]{40,}={0,2}"      # base64
    r"|[a-f0-9]{40,}"                    # hex
    r"|\?[^=]+=[^&\s]{200,})",          # very long query value
    re.IGNORECASE,
)
_SUSPICIOUS_TLDS: tuple[str, ...] = (
    ".tk", ".ml", ".ga", ".cf", ".gq", ".pw", ".top",
)

_SANDBOX_ESCAPE = re.compile(
    r"docker\.sock"
    r"|/var/run/docker\.sock"
    r"|\bdocker\s+run\b[^\n]*--privileged\b"
    r"|--cap-add=?\s*(?:ALL|SYS_ADMIN|SYS_PTRACE|NET_ADMIN)"
    r"|nsenter\b"
    r"|/proc/1/root"
    r"|sysctl\s+-w\b"
    r"|capsh\s+--print\b"
    r"|\bsetcap\s+(?:cap_)?[a-z_]+\+"
    r"|\bchroot\s+/"
    r"|mount\s+--bind\s+/",
    re.IGNORECASE,
)

_PROMPT_INJECTION = re.compile(
    r"\b(?:"
    r"ignore (?:all |every |the )?(?:previous|prior|above) "
    r"(?:instructions|context|system)"
    r"|disregard (?:all|prior|previous) (?:instructions|rules)"
    r"|forget (?:everything|all instructions)"
    r"|system: you are now"
    r"|new instructions:\s+system"
    r"|<\|im_start\|>system"
    r"|\[INST\]\s*system"
    r")",
    re.IGNORECASE,
)

_MCP_INJECTION = re.compile(
    r"\b(?:"
    r"when (?:invoked|called),? (?:also|always) (?:run|execute|fetch)"
    r"|(?:before|after) (?:returning|responding|completing),? (?:send|exfil)"
    r"|on every (?:call|invocation),? (?:include|attach)"
    r"|as part of (?:this|the) tool,? (?:also|additionally)"
    r")",
    re.IGNORECASE,
)

_GIT_FORCE_PUSH = re.compile(
    r"\bgit\s+push\b.*?(?:--force|--force-with-lease|(?<!\S)-f)\b.*?"
    r"\b(main|master|prod|production|release)\b"
    r"|\bgit\s+push\b.*?\b(main|master|prod|production|release)\b.*?"
    r"(?:--force|--force-with-lease|(?<!\S)-f)\b",
    re.IGNORECASE | re.DOTALL,
)
_GIT_FORCE_DELETE = re.compile(
    r"\bgit\s+branch\b.*?(?<!\S)(?:-D|--delete\s+--force)\b\s+"
    r"(main|master|prod|production|release)\b",
    re.IGNORECASE,
)
_GIT_REBASE_MAIN = re.compile(
    r"\bgit\s+rebase\b(?!.*--abort)(?!.*--continue).*\b(main|master)\b",
    re.IGNORECASE,
)

# Default per-tool USD ceilings (donor: payment_overflow.DEFAULT_CEILINGS)
_PAYMENT_CEILINGS: dict[str, float] = {
    "stripe_charge": 1_000.0,
    "wire_transfer": 10_000.0,
    "ach_payment": 5_000.0,
    "crypto_send": 500.0,
    "payout": 5_000.0,
}
_PAYMENT_AMOUNT_KEYS: tuple[str, ...] = (
    "amount", "amount_usd", "amount_cents", "value", "total",
)

# ─────────────────────────────────────────────────────────────────────
# Rule entry points
# ─────────────────────────────────────────────────────────────────────
_BLOCK = ("BLOCK", "block")
_APPROVE = ("REQUIRE_APPROVAL", "require_approval")


def _check_persona_drift(tool: str, args_text: str) -> tuple[str, str] | None:
    if _PERSONA_DRIFT.search(args_text):
        return ("REQUIRE_APPROVAL", "rule:persona_drift")
    return None


def _check_exfil_url(tool: str, args_text: str) -> tuple[str, str] | None:
    if tool not in _EXFIL_TOOLS:
        return None
    if _EXFIL_URL.search(args_text):
        return ("BLOCK", "rule:exfil_url (encoded payload)")
    lower = args_text.lower()
    if any(tld in lower for tld in _SUSPICIOUS_TLDS):
        return ("BLOCK", "rule:exfil_url (suspicious TLD)")
    return None


def _check_sandbox_escape(tool: str, args_text: str) -> tuple[str, str] | None:
    if tool not in _SHELL_TOOLS:
        return None
    if _SANDBOX_ESCAPE.search(args_text):
        return ("BLOCK", "rule:sandbox_escape")
    return None


def _check_prompt_injection(tool: str, args_text: str) -> tuple[str, str] | None:
    if tool not in _PROMPT_INJECTION_TOOLS:
        return None
    if _PROMPT_INJECTION.search(args_text):
        return ("REQUIRE_APPROVAL", "rule:prompt_injection")
    return None


def _check_mcp_injection(tool: str, args_text: str) -> tuple[str, str] | None:
    if tool not in _MCP_REGISTRATION_TOOLS:
        return None
    if _MCP_INJECTION.search(args_text):
        return ("BLOCK", "rule:mcp_injection")
    return None


def _check_git_destructive(tool: str, args_text: str) -> tuple[str, str] | None:
    if tool not in _GIT_TOOLS:
        return None
    if (
        _GIT_FORCE_PUSH.search(args_text)
        or _GIT_FORCE_DELETE.search(args_text)
        or _GIT_REBASE_MAIN.search(args_text)
    ):
        return ("BLOCK", "rule:git_destructive")
    return None


def _check_payment_overflow(
    tool: str, args_obj: dict[str, Any]
) -> tuple[str, str] | None:
    ceiling = _PAYMENT_CEILINGS.get(tool)
    if ceiling is None:
        return None
    for k in _PAYMENT_AMOUNT_KEYS:
        if k not in args_obj:
            continue
        try:
            v = float(args_obj[k])
        except (TypeError, ValueError):
            continue
        if k.endswith("_cents"):
            v = v / 100.0
        if v >= ceiling:
            return (
                "BLOCK",
                f"rule:payment_overflow ({tool}={v:.2f} >= {ceiling:.2f} USD)",
            )
    return None


def _parse_args(tool_args_json: str) -> dict[str, Any]:
    try:
        parsed = json.loads(tool_args_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def run(
    atv: np.ndarray, inp: ATVInput, ctx: FirewallContext
) -> StepResult:
    """Run the donor pattern rule pack.

    First match short-circuits the rest of the pack; further pipeline
    steps are still invoked by the orchestrator only on ALLOW (None).
    """
    args_text = inp.tool_args_json or ""
    args_obj = _parse_args(args_text)
    tool = inp.tool_name

    text_rules = (
        _check_persona_drift,
        _check_exfil_url,
        _check_sandbox_escape,
        _check_prompt_injection,
        _check_mcp_injection,
        _check_git_destructive,
    )
    for rule in text_rules:
        hit = rule(tool, args_text)
        if hit is not None:
            verdict, reason = hit
            return StepResult(
                verdict=verdict, reason=reason, trace=f"step311: {reason}"
            )

    payment_hit = _check_payment_overflow(tool, args_obj)
    if payment_hit is not None:
        verdict, reason = payment_hit
        return StepResult(verdict=verdict, reason=reason, trace=f"step311: {reason}")

    return StepResult(
        verdict=None, reason="", trace="step311: no donor rule matched"
    )
