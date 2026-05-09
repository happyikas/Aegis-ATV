"""Per-token logprob aggregation for the OpenClaw + Local OSS LLM
forensic surface.

vLLM's ``--return-logprobs`` flag (and the matching OpenAI-compatible
``logprobs`` request param) returns per-token logprobs alongside the
generated text:

.. code-block:: json

    {
      "logprobs": [
        {"token": "Hello", "logprob": -0.012, "top_logprobs": [...]},
        {"token": " world", "logprob": -0.003, "top_logprobs": [...]},
        ...
      ]
    }

Storing the full per-token list per audit record is expensive — a
typical 500-token response has ~50 KB of logprobs. Aegis instead
stores an *aggregate* (LogitMetrics) plus a tiny sample of the
lowest-confidence tokens (forensic surface). The full per-token
trace stays in the operator's vLLM logs if they need to drill down.

This module is **only meaningful in the OpenClaw + Local OSS LLM
release track**. Cloud LLM tracks (Claude Code, OpenClaw + Cloud
LLM) do not expose per-token logprobs. See docs/releases/
OPENCLAW_LOCAL.ko.md §2 for the positioning.

Usage in the firewall:

    >>> from aegis.inference.logit_metrics import parse_vllm_logprobs
    >>> metrics = parse_vllm_logprobs(vllm_response_json["logprobs"])
    >>> # store metrics.to_dict() in audit_record.explain.logit_metrics
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# Default low-confidence threshold (in nats). A logprob of ≤ -2.3 means
# the model assigned ≤ ~10% probability — meaningful uncertainty.
DEFAULT_LOW_CONFIDENCE_THRESHOLD = -2.3
# Cap on how many lowest-confidence tokens we sample for the forensic
# surface. Larger windows blow up audit-record size; 5 is enough for
# operators to spot the suspicious region without paging.
DEFAULT_SAMPLE_CAP = 5


@dataclass(frozen=True)
class LogitMetrics:
    """Aggregated logprob signal for a single LLM response.

    Attributes
    ----------
    n_tokens
        Total tokens in the response.
    mean_logprob
        Arithmetic mean of all token logprobs. Higher = more confident.
        Typical "well-behaved" responses sit around -0.5 to -0.2.
    min_logprob
        Logprob of the single least-confident token. Useful as a
        canary — sustained low-confidence streaks elevate hallucination
        risk.
    n_low_confidence_tokens
        Count of tokens with ``logprob < low_confidence_threshold``.
        Caller picks the threshold (default -2.3 nats ≈ 10% prob).
    low_confidence_threshold
        The threshold actually used for ``n_low_confidence_tokens``.
        Recorded so downstream consumers can compare across runs that
        used different thresholds.
    sample_low_confidence_tokens
        Up to ``DEFAULT_SAMPLE_CAP`` (token, logprob) tuples for the
        lowest-confidence tokens — sufficient for ``aegis forensic
        --logits`` to point at the suspicious regions without
        storing the full per-token trace.
    """

    n_tokens: int
    mean_logprob: float
    min_logprob: float
    n_low_confidence_tokens: int
    low_confidence_threshold: float = DEFAULT_LOW_CONFIDENCE_THRESHOLD
    sample_low_confidence_tokens: list[tuple[str, float]] = field(
        default_factory=list,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_tokens": self.n_tokens,
            "mean_logprob": self.mean_logprob,
            "min_logprob": self.min_logprob,
            "n_low_confidence_tokens": self.n_low_confidence_tokens,
            "low_confidence_threshold": self.low_confidence_threshold,
            "sample_low_confidence_tokens": [
                {"token": t, "logprob": lp}
                for t, lp in self.sample_low_confidence_tokens
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LogitMetrics:
        sample_raw = data.get("sample_low_confidence_tokens") or []
        sample: list[tuple[str, float]] = []
        for item in sample_raw:
            if isinstance(item, dict):
                tok = str(item.get("token", ""))
                lp = float(item.get("logprob", 0.0))
                sample.append((tok, lp))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                sample.append((str(item[0]), float(item[1])))
        return cls(
            n_tokens=int(data.get("n_tokens", 0) or 0),
            mean_logprob=float(data.get("mean_logprob", 0.0) or 0.0),
            min_logprob=float(data.get("min_logprob", 0.0) or 0.0),
            n_low_confidence_tokens=int(
                data.get("n_low_confidence_tokens", 0) or 0,
            ),
            low_confidence_threshold=float(
                data.get(
                    "low_confidence_threshold",
                    DEFAULT_LOW_CONFIDENCE_THRESHOLD,
                ) or DEFAULT_LOW_CONFIDENCE_THRESHOLD,
            ),
            sample_low_confidence_tokens=sample,
        )

    def confidence_band(self) -> str:
        """Single-word band for dashboards.

        Maps the *fraction* of low-confidence tokens to a band so it's
        comparable across responses of different lengths:

        * ``high``     — < 5% of tokens were low-confidence
        * ``moderate`` — < 15%
        * ``low``      — < 30%
        * ``critical`` — ≥ 30% (sustained uncertainty; hallucination risk)
        """
        if self.n_tokens <= 0:
            return "unknown"
        frac = self.n_low_confidence_tokens / self.n_tokens
        if frac < 0.05:
            return "high"
        if frac < 0.15:
            return "moderate"
        if frac < 0.30:
            return "low"
        return "critical"

    def hallucination_risk(self) -> bool:
        """True iff confidence band is ``low`` or ``critical``.

        ``aegis advise`` (Doctor) escalates to a [HIGH] signal when
        this is true, prompting forensic inspection of the
        low-confidence sample.
        """
        return self.confidence_band() in ("low", "critical")


# ──────────────────────────────────────────────────────────────────
# Parser — vLLM ``logprobs`` response shape
# ──────────────────────────────────────────────────────────────────


def parse_vllm_logprobs(
    logprobs_payload: list[dict[str, Any]] | None,
    *,
    low_confidence_threshold: float = DEFAULT_LOW_CONFIDENCE_THRESHOLD,
    sample_cap: int = DEFAULT_SAMPLE_CAP,
) -> LogitMetrics | None:
    """Convert a vLLM logprobs response array into :class:`LogitMetrics`.

    Returns ``None`` for an empty / missing payload (caller skips
    persistence). Raises :class:`ValueError` when the payload is
    structurally invalid (caller can downgrade to None or surface
    the error — Aegis's audit pipeline does the former).

    The parser is tolerant of two near-identical schemas:

    * vLLM native ``[{"token": str, "logprob": float, ...}]``
    * OpenAI-compat ``[{"text": str, "token_logprobs": [-0.1, ...]}]``
      — used by some vLLM configurations. We fold the second shape
      into the first internally.
    """
    if not logprobs_payload:
        return None

    # Detect + flatten OpenAI-compat shape (single object with parallel
    # text + token_logprobs arrays).
    if (
        isinstance(logprobs_payload, list)
        and len(logprobs_payload) >= 1
        and isinstance(logprobs_payload[0], dict)
        and "token_logprobs" in logprobs_payload[0]
    ):
        flattened: list[dict[str, Any]] = []
        for chunk in logprobs_payload:
            tokens = chunk.get("tokens") or []
            lps = chunk.get("token_logprobs") or []
            for tok, lp in zip(tokens, lps, strict=False):
                if lp is None:
                    continue
                flattened.append({"token": tok, "logprob": lp})
        logprobs_payload = flattened

    if not isinstance(logprobs_payload, list):
        raise ValueError(
            "logprobs payload must be a list of {token, logprob} entries"
        )

    n_tokens = 0
    sum_logprob = 0.0
    min_logprob = math.inf
    low_confidence_tokens: list[tuple[str, float]] = []

    for entry in logprobs_payload:
        if not isinstance(entry, dict):
            continue
        try:
            lp = float(entry["logprob"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isnan(lp) or math.isinf(lp):
            # Skip pathological entries (vLLM emits -inf for "this token
            # was sampled but the kernel had numerical issues"); they
            # would skew the mean.
            continue
        tok = str(entry.get("token", ""))
        n_tokens += 1
        sum_logprob += lp
        if lp < min_logprob:
            min_logprob = lp
        if lp < low_confidence_threshold:
            low_confidence_tokens.append((tok, lp))

    if n_tokens == 0:
        return None

    # Bottom-N lowest-confidence tokens for the forensic sample.
    low_confidence_tokens.sort(key=lambda kv: kv[1])
    sample = low_confidence_tokens[:sample_cap]

    return LogitMetrics(
        n_tokens=n_tokens,
        mean_logprob=sum_logprob / n_tokens,
        min_logprob=(
            min_logprob if not math.isinf(min_logprob) else 0.0
        ),
        n_low_confidence_tokens=len(low_confidence_tokens),
        low_confidence_threshold=low_confidence_threshold,
        sample_low_confidence_tokens=sample,
    )
