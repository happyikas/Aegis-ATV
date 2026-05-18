"""Step 311 — Donor pattern rule pack + v2.1.2 cloud destructive.

Ports the 7 stdlib-only pattern rules from
``_donor/aegis-mvp/atmu/rules/`` (D11) plus two v2.1.2 expansions:

    persona_drift       I-01  → REQUIRE_APPROVAL    (D11)
    exfil_url           I-04  → BLOCK               (D11)
    sandbox_escape      I-06  → BLOCK               (D11)
    exfil_url (DNS)     I-07  → BLOCK               (D11, same rule)
    prompt_injection    I-08  → REQUIRE_APPROVAL    (D11)
    mcp_injection       I-09  → BLOCK               (D11)
    git_destructive     I-10  → BLOCK               (D11)
    payment_overflow    I-11  → BLOCK               (D11)
    cloud_destructive         → BLOCK               (v2.1.2)
    sql_unbounded             → BLOCK               (v2.1.2)
    aegis_self_modification   → BLOCK               (Solo-Free)

cloud_destructive covers kubectl / terraform / aws / gcloud / az /
helm / docker destructive operations — the Day-1 #2 expansion from
the must-install backlog. sql_unbounded catches DELETE / UPDATE
statements without a WHERE clause that would mutate every row.
aegis_self_modification blocks shell-level attempts to stop / kill
the plugin firewall, destroy the audit chain, or toggle autonomy off
— closing the Solo-Free dummy-judge coverage gap.

Two donor rules are intentionally NOT included here because they need
runtime context that is not yet ported under Phase 2:

* ``cost_overflow``      — depends on D10 ``cost.budget`` + per-day
  spend tracker (planned for v2.1.3 loop detector follow-up).
* ``malfunction_pattern`` — depends on D7 ``monitor.malfunction``
  (planned for v2.1.3).

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

# v2.1.2 — cloud destructive patterns (Day-1 #2 expansion)
_CLOUD_DESTRUCTIVE = re.compile(
    # Kubernetes
    r"\bkubectl\s+delete\b"
    r"|\bkubectl\s+drain\b"
    r"|\bkubectl\s+(?:cordon|uncordon)\b\s+\S+\s+--force"
    # Terraform
    r"|\bterraform\s+(?:destroy|apply\s+-auto-approve)\b"
    r"|\bterraform\s+state\s+rm\b"
    # AWS CLI mutating
    r"|\baws\s+s3\s+rm\s+s3://"
    r"|\baws\s+s3\s+rb\s+s3://"
    r"|\baws\s+iam\s+(?:delete|detach|put|create-access-key|create-user|attach-user-policy|update-assume-role-policy)\b"
    r"|\baws\s+ec2\s+terminate-instances\b"
    r"|\baws\s+rds\s+(?:delete-db-(?:instance|cluster|snapshot)|stop-db-cluster)\b"
    # GCP / gcloud
    r"|\bgcloud\s+iam\s+(?:roles|service-accounts)\s+delete\b"
    r"|\bgcloud\s+iam\s+service-accounts\s+keys\s+create\b"
    r"|\bgcloud\s+(?:compute|sql|kms)\s+\S+\s+delete\b"
    r"|\bgcloud\s+projects\s+(?:delete|remove-iam-policy-binding)\b"
    # Azure
    r"|\baz\s+role\s+assignment\s+(?:create|delete)\b"
    r"|\baz\s+(?:vm|sql|storage|keyvault)\s+\S*\s*delete\b"
    # Helm
    r"|\bhelm\s+(?:uninstall|delete)\b"
    # Docker
    r"|\bdocker\s+(?:rmi\s+-f|system\s+prune\s+-a|volume\s+rm\b)",
    re.IGNORECASE,
)

# v2.1.2 — unbounded SQL mutations (DELETE / UPDATE without WHERE)
# Permits the WHERE clause anywhere after the table name; flags only when
# absent. Multi-line DOTALL so multiline SQL still trips.
_SQL_UNBOUNDED_DELETE = re.compile(
    r"\bDELETE\s+FROM\s+\S+(?!.*\bWHERE\b)(?:\s*;|\s*$)",
    re.IGNORECASE | re.DOTALL,
)
_SQL_UNBOUNDED_UPDATE = re.compile(
    r"\bUPDATE\s+\S+\s+SET\s+\S+\s*=\s*\S+(?!.*\bWHERE\b)(?:\s*;|\s*$)",
    re.IGNORECASE | re.DOTALL,
)

# Aegis self-modification defense — Solo-Free coverage closure.
#
# The dummy sLLM judge cannot semantically detect "agent is trying to
# disable its own security infrastructure" the way the Haiku judge can
# (CLAUDE.md §"Plugin Mode 작업 시"). When ``AEGIS_JUDGE_PROVIDER=dummy``
# is in effect that dynamic line of defense is absent — so block the
# concrete shell patterns statically:
#
# * Service control aimed at any process named ``aegis``:
#     launchctl stop/disable/unload, systemctl stop/disable/kill,
#     pkill / killall / kill $(pgrep ...).
# * Destruction of the audit chain or plugin hook config:
#     rm / mv / truncate against ``~/.aegis/**`` or
#     ``~/.claude/settings.json``.
# * Toggling autonomy off via the shell:
#     export AEGIS_AUTONOMY_ENABLED=0, unset AEGIS_AUTONOMY_ENABLED.
#
# Legitimate ``aegis ...`` CLI flows (install / uninstall / baseline)
# do not trip — they invoke the binary, not direct rm / launchctl.
_AEGIS_SELF_MODIFICATION = re.compile(
    # Service control: launchctl stop|disable|unload ... <aegis>
    r"\b(?:launchctl|systemctl)\s+(?:--user\s+)?"
    r"(?:stop|disable|unload|kill)\b[^\n]*\baegis\b"
    # Process-kill family targeting aegis
    r"|\b(?:pkill|killall)\b[^\n]*\baegis"
    r"|\bkill\s+(?:-[A-Z0-9]+\s+)?\$\(\s*pgrep\b[^)]*aegis"
    # Destruction of the audit chain / autonomy state directory
    r"|\b(?:rm|unlink|mv)\s+(?:-[a-zA-Z]+\s+)*[^\s|;&]*\.aegis(?:/|\b)"
    # Destruction or replacement of the plugin hook config
    r"|\b(?:rm|unlink|mv)\s+(?:-[a-zA-Z]+\s+)*"
    r"[^\s|;&]*\.claude/settings(?:\.local)?\.json\b"
    # Truncate / overwrite the plugin hook config (any redirect to it)
    r"|>\s*[~./]*\.claude/settings(?:\.local)?\.json\b"
    # Edit the in-tree hook entry point
    r"|\b(?:rm|mv)\s+[^\s|;&]*tools/aegis_local_hook\.py\b"
    # Toggle autonomy off via shell env mutation
    r"|\bexport\s+AEGIS_AUTONOMY_ENABLED\s*=\s*0\b"
    r"|\bunset\s+AEGIS_AUTONOMY_ENABLED\b",
    re.IGNORECASE,
)

_SQL_TOOLS: frozenset[str] = frozenset({
    "sql", "execute_sql", "database_query", "db_query", "query_db",
    "db_mutation", "psql", "mysql",
})

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


def _check_cloud_destructive(tool: str, args_text: str) -> tuple[str, str] | None:
    """v2.1.2 — kubectl/terraform/aws/gcloud/az/helm/docker destructive ops."""
    if tool not in _SHELL_TOOLS:
        return None
    if _CLOUD_DESTRUCTIVE.search(args_text):
        return ("BLOCK", "rule:cloud_destructive")
    return None


def _check_aegis_self_modification(
    tool: str, args_text: str,
) -> tuple[str, str] | None:
    """Block shell commands that try to stop / kill / unconfigure the
    plugin firewall itself (Solo-Free dummy-judge coverage closure).

    The Haiku judge already semantically catches "self-modification of
    security infrastructure" — this rule is the static counterpart for
    when ``AEGIS_JUDGE_PROVIDER=dummy``.
    """
    if tool not in _SHELL_TOOLS:
        return None
    if _AEGIS_SELF_MODIFICATION.search(args_text):
        return ("BLOCK", "rule:aegis_self_modification")
    return None


def _check_sql_unbounded(tool: str, args_text: str) -> tuple[str, str] | None:
    """v2.1.2 — unbounded DELETE / UPDATE on SQL-class tools."""
    if tool not in _SQL_TOOLS and tool not in _SHELL_TOOLS:
        return None
    if _SQL_UNBOUNDED_DELETE.search(args_text):
        return ("BLOCK", "rule:sql_unbounded (DELETE without WHERE)")
    if _SQL_UNBOUNDED_UPDATE.search(args_text):
        return ("BLOCK", "rule:sql_unbounded (UPDATE without WHERE)")
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
        _check_cloud_destructive,
        _check_aegis_self_modification,
        _check_sql_unbounded,
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
