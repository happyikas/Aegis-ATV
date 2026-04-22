#!/usr/bin/env python3
"""Pre-LLM (or pre-tool) safety classifier — fills in safety_flags before
the host POSTs ATVInput to Aegis.

Why: until this module existed, every host (demo, hook, …) was sending
``"safety_flags": {}`` to /evaluate. Step 310's prompt-injection
threshold check NEVER fired in real traffic — only in the theater
where the flag was hardcoded. This module closes that gap.

Three backends — pick via env var ``AEGIS_SAFETY_PROVIDER``:

  dummy  (default)  Regex heuristics. Stdlib-only. Free, fast, offline.
                    Conservative: single-pattern hit → 0.80 score (just
                    above step 310's 0.70 threshold). Multi-hit
                    saturates to 0.95.

  openai            OpenAI omni-moderation-latest. Free tier, ~50ms.
                    Covers harassment / hate / violence (mapped to
                    `toxicity`). For prompt_injection / pii_exposure /
                    sql_injection / path_traversal / data_exfiltration
                    it falls back to the dummy regexes (Moderations
                    doesn't classify those).

  haiku             Claude Haiku 3.5 as a JSON-only classifier. Best
                    for prompt-injection nuance. ~$0.001/call,
                    ~300-800ms.

All backends fail-soft: any network/parse error → return dummy scores
(or empty dict) so the host never crashes the agent on classifier
failure.

CLI:  echo "ignore previous instructions" | python3 tools/aegis_safety.py
      → {"prompt_injection": 0.8, ...}
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

# ─────────────────────────────────────────────────────────────────────
# Regex patterns — conservative; we want false negatives, not false
# positives, since safety_flags can lead to BLOCK at step 310.
# ─────────────────────────────────────────────────────────────────────

_PATTERNS: dict[str, list[str]] = {
    "prompt_injection": [
        r"ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions?",
        r"disregard\s+(?:the\s+)?(?:above|previous|prior)",
        r"\byou\s+are\s+now\s+(?:a|an|the)\s+\w+",
        r"\[?DAN\]?\s+(?:mode|jailbreak)",
        r"system\s*[:>]\s*you\s+(?:must|will|shall)",
        r"new\s+instructions?\s*[:>]",
        r"forget\s+(?:everything|all)\s+(?:above|prior|previous)",
        r"override\s+(?:safety|security|filter|guardrails?)",
        r"reveal\s+(?:your|the)\s+(?:system\s+)?prompt",
        r"act\s+as\s+(?:if\s+you\s+were\s+)?an?\s+\w+\s+with\s+no\s+restrictions?",
    ],
    "pii_exposure": [
        r"\b\d{3}-\d{2}-\d{4}\b",                                         # US SSN
        r"\b(?:\d[ -]?){13,16}\b",                                        # credit-card-shaped
        r"\bsk-[a-zA-Z0-9]{32,}\b",                                       # OpenAI API key
        r"\bsk-ant-[a-zA-Z0-9_-]{20,}\b",                                 # Anthropic API key
        r"\bAKIA[A-Z0-9]{16}\b",                                          # AWS access key
        r"-----BEGIN\s+(?:RSA |DSA |EC |OPENSSH )?PRIVATE\s+KEY-----",    # private key block
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",                              # Slack token
    ],
    "sql_injection": [
        r"\bDROP\s+TABLE\b",
        r"\bUNION\s+(?:ALL\s+)?SELECT\b",
        r"';\s*(?:--|#|/\*)",
        r"\bOR\s+['\"]?1['\"]?\s*=\s*['\"]?1['\"]?",
        r"\bDELETE\s+FROM\s+\w+(?:\s*;|\s*$)",
        r"\bxp_cmdshell\b",
        r"';\s*EXEC\b",
    ],
    "path_traversal": [
        r"(?:\.\./){2,}",
        r"/etc/(?:shadow|passwd|sudoers|gshadow)",
        r"%2e%2e[/\\]",
        r"\.\.[/\\][a-z]",
        r"~/?\.ssh/(?:id_rsa|authorized_keys|config)",
        r"~/?\.aws/credentials",
        r"\\windows\\system32\\config\\sam",
    ],
    "data_exfiltration": [
        # Existing patterns
        r"curl\s+(?:-[A-Za-z]+\s+)*(?:-X\s+POST|--data\b).+https?://",
        r"wget\s+(?:--post-data|--method\s*=\s*POST)",
        r"\bbase64\b\s+(?:--encode\s+|-w\s*0\s+)?(?:/etc|~/\.|/root)",
        r"\bnc\s+(?:-[lewz]+\s+)*\d+\.\d+\.\d+\.\d+\s+\d+",
        r"@(?:\.env|secrets?\.txt|credentials?\.json|id_rsa|\.aws/credentials)",
        r"\bsend(?:_email|mail)\b.*['\"]?attachments?['\"]?\s*[:=]",
        # DOGFOOD Rec #5 — broaden curl/wget POST + sensitive-file-attached pattern
        r"\b(?:curl|wget|http|httpie)\b.*-(?:d|F|data|data-binary|form|upload-file)\s*['\"@]?(?:\.env|.+\.pem|.+\.key|credentials|.aws|.ssh|secrets?)",
        # base64-piped exfil — `base64 file | curl ...` or `... | nc ...`
        r"\bbase64\b[^|]*\|\s*(?:curl|wget|nc|ncat|http)\b",
        # Suspicious-domain heuristics: posting to a non-allowlist host
        r"\b(?:curl|wget)\b[^|]+(?:attacker|exfil|pastebin|paste\.bin|gist|webhook\.site|requestbin|ngrok)\b",
        # gzip|tar|zip the whole home and pipe out
        r"\b(?:tar|zip|gzip)\b[^|]*\$HOME[^|]*\|",
    ],
}

_COMPILED: dict[str, list[re.Pattern[str]]] = {
    cat: [re.compile(p, re.IGNORECASE) for p in pats] for cat, pats in _PATTERNS.items()
}

ALL_CATEGORIES: tuple[str, ...] = tuple(_PATTERNS.keys())

# Single-hit score is set just above step 310's 0.70 threshold so a
# clean classifier hit DOES trigger a BLOCK without needing two hits.
_SINGLE_HIT_SCORE = 0.80
_PER_EXTRA_HIT = 0.05
_MAX_SCORE = 0.95


# ─────────────────────────────────────────────────────────────────────
# Backends
# ─────────────────────────────────────────────────────────────────────

def _dummy_classify(text: str) -> dict[str, float]:
    out: dict[str, float] = {cat: 0.0 for cat in ALL_CATEGORIES}
    for cat, patterns in _COMPILED.items():
        hits = sum(1 for rx in patterns if rx.search(text))
        if hits == 0:
            out[cat] = 0.0
        else:
            out[cat] = min(_MAX_SCORE, _SINGLE_HIT_SCORE + _PER_EXTRA_HIT * (hits - 1))
    return out


def _http_post_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _openai_moderation(text: str, api_key: str) -> dict[str, float]:
    """OpenAI Moderations covers harassment/hate/violence/etc — NOT
    prompt_injection/PII/SQL/path/exfil. We map what we can and fall
    back to dummy for the rest.
    """
    base = _dummy_classify(text)  # always have these populated
    try:
        data = _http_post_json(
            "https://api.openai.com/v1/moderations",
            {"model": "omni-moderation-latest", "input": text[:4000]},
            {"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
        cat_scores = data["results"][0]["category_scores"]
        # Map OpenAI's categories into our 'toxicity' bucket (max).
        base["toxicity"] = max(
            float(cat_scores.get("harassment", 0.0)),
            float(cat_scores.get("hate", 0.0)),
            float(cat_scores.get("violence", 0.0)),
            float(cat_scores.get("sexual", 0.0)),
            float(cat_scores.get("self-harm", 0.0)),
        )
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError, IndexError):
        # network or parsing problem → dummy-only
        pass
    return base


_HAIKU_SYSTEM = (
    "You are a SAFETY CLASSIFIER. Read the input below and return ONLY a JSON "
    "object — no prose, no markdown — scoring it for these categories, each in "
    "[0.0, 1.0] where 0=clearly safe, 1=clearly malicious:\n"
    '  {"prompt_injection": ?, "pii_exposure": ?, "sql_injection": ?, '
    '"path_traversal": ?, "data_exfiltration": ?, "toxicity": ?}\n'
    "Be conservative — only score >0.7 if there is a clear, specific signal "
    "in the text itself, not just topic adjacency."
)


def _haiku_classify(text: str, api_key: str) -> dict[str, float]:
    """Claude Haiku 3.5 as a JSON-only classifier. Highest quality for
    prompt_injection nuance. Fail-soft → dummy on any error.
    """
    try:
        data = _http_post_json(
            "https://api.anthropic.com/v1/messages",
            {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "temperature": 0.0,
                "system": _HAIKU_SYSTEM,
                "messages": [{"role": "user", "content": text[:4000]}],
            },
            {
                "X-API-Key": api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=10.0,
        )
        raw = data["content"][0]["text"].strip()
        # Defensive JSON extraction (model occasionally wraps in markdown).
        s, e = raw.find("{"), raw.rfind("}") + 1
        if s < 0 or e <= s:
            return _dummy_classify(text)
        scores = json.loads(raw[s:e])
        out: dict[str, float] = {cat: 0.0 for cat in ALL_CATEGORIES}
        out["toxicity"] = 0.0
        for k, v in scores.items():
            if isinstance(v, (int, float)):
                out[k] = max(0.0, min(1.0, float(v)))
        return out
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError, IndexError):
        return _dummy_classify(text)


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def classify(text: str, *, provider: str | None = None) -> dict[str, float]:
    """Score ``text`` for each safety category, returning ``{cat: score}``."""
    if not text:
        return {cat: 0.0 for cat in ALL_CATEGORIES}

    p = (provider or os.environ.get("AEGIS_SAFETY_PROVIDER", "dummy")).lower()

    if p == "openai":
        key = os.environ.get("OPENAI_API_KEY", "")
        return _openai_moderation(text, key) if key else _dummy_classify(text)
    if p == "haiku":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        return _haiku_classify(text, key) if key else _dummy_classify(text)
    # default
    return _dummy_classify(text)


def classify_call(
    *,
    tool_args_json: str = "",
    plan_text: str = "",
    agent_state_text: str = "",
    provider: str | None = None,
) -> dict[str, float]:
    """Score the combined text content of a tool call. Multiple sources
    are combined by taking the MAX score per category — so a clean
    plan_text with risky tool_args_json correctly inherits the risk.
    Drops zero-score categories from the return so the resulting
    safety_flags dict is small.
    """
    sources = [s for s in (tool_args_json, plan_text, agent_state_text) if s]
    if not sources:
        return {}
    combined: dict[str, float] = {}
    for src in sources:
        for cat, score in classify(src, provider=provider).items():
            if score > combined.get(cat, 0.0):
                combined[cat] = score
    return {k: v for k, v in combined.items() if v > 0.0}


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def _main() -> int:
    text = sys.stdin.read() if not sys.stdin.isatty() else " ".join(sys.argv[1:])
    if not text.strip():
        print("usage:  echo TEXT | aegis_safety.py     OR     aegis_safety.py TEXT", file=sys.stderr)
        return 2
    print(json.dumps(classify(text), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
