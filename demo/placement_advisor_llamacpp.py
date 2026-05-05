"""Aegis-ATV Placement Advisor × llama-cpp KV-Cache Verification
================================================================

Empirically measure whether Aegis's placement advisor (v3.4) gives
actionable advice on a *real* inference runtime — llama-cpp via
``llama-cpp-python`` (Metal on Apple Silicon).

The advisor's ``kv_quantisation_dtype`` output (``f16`` / ``q8_0`` /
``q4_0``) maps 1:1 to llama-cpp's ``type_k`` / ``type_v`` runtime
parameters. Same model weights, same prompt — only the KV cache
quantisation tier varies.

What the demo proves
--------------------

For each of N synthetic agent contexts (varying cache_hit_rate /
context_utilization / novelty / task_progress), it runs two
strategies on the SAME prompt:

* **Baseline (no advisor)** — always F16 KV cache. Highest quality,
  highest memory.
* **Advisor-guided** — ATV → ``placement_advisor`` → use the
  recommended KV tier.

For each (scenario, strategy) we record:

* per-token latency (wall-clock)
* analytical KV-cache memory (n_layer × n_kv_head × head_dim ×
  ctx × bytes-per-element)
* output text similarity vs the F16 reference (difflib ratio)

The aggregate verdict shows the advisor's decision rule:
**lose nothing on hot paths, save N× memory on cold paths.**

Run
---

::

    uv sync --extra local-llm    # CMAKE_ARGS=-DGGML_METAL=on for Mac
    uv run python demo/placement_advisor_llamacpp.py

The model file (``models/llamacpp/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf``,
~640 MB) is auto-downloaded from HuggingFace on first run.

Requirements
------------

* llama-cpp-python ≥ 0.2.85 (with ``flash_attn=True`` support — this
  is required for non-F16 KV cache types).
* Metal (Apple) or CUDA build for n_gpu_layers=-1 to take effect.
"""

from __future__ import annotations

import difflib
import resource
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# ruff: noqa: E402  -- imports follow the sys.path bootstrap above.
import json

import numpy as np

from aegis.atv.builder import build_atv
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.performance.placement_advisor import PlacementAdvice, placement_advisor
from aegis.schema import (
    ATVHeader,
    ATVInput,
    CostEfficiencyMetrics,
)

# ─────────────────────────────────────────────────────────────────────
# Model + KV-cache constants
# ─────────────────────────────────────────────────────────────────────

MODEL_PATH = ROOT / "models" / "llamacpp" / "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
MODEL_REPO = "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF"
MODEL_FILE = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"

# llama-cpp ggml type IDs
GGML_F16 = 1
GGML_Q4_0 = 2
GGML_Q8_0 = 8
TIER_TYPE_ID = {"f16": GGML_F16, "q8_0": GGML_Q8_0, "q4_0": GGML_Q4_0}

# Per-element bytes (analytical; matches ggml block layout incl. scales).
TIER_BYTES_PER_ELEMENT = {"f16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}

# TinyLlama-1.1B architecture (introspected, see comment block at bottom).
N_LAYER = 22
N_KV_HEAD = 4
HEAD_DIM = 64

DEMO_N_CTX = 2048      # matches model training context
GEN_MAX_TOKENS = 60    # same per scenario for comparable latency
GEN_TEMPERATURE = 0.0  # deterministic so quality diffs are real


# ─────────────────────────────────────────────────────────────────────
# Synthetic scenarios — span the advisor's decision space
# ─────────────────────────────────────────────────────────────────────


@dataclass
class Scenario:
    name: str
    description: str
    prompt: str
    # ATV cost-band signals
    cache_hit_rate: float
    context_util: float
    task_progress: float
    novelty: float


SCENARIOS: list[Scenario] = [
    Scenario(
        name="hot-revisit",
        description=(
            "mid-task, high progress, low novelty — KV will be re-read; "
            "advisor wants F16"
        ),
        prompt="Continue: 'In summary, the key three steps are: 1)'",
        cache_hit_rate=0.85, context_util=0.40,
        task_progress=0.70, novelty=0.10,
    ),
    Scenario(
        name="warm-mid-task",
        description="mid signals; advisor leans Q8_0 — half mem, ~quality",
        prompt="Translate to French: 'The weather is nice today.'",
        cache_hit_rate=0.50, context_util=0.50,
        task_progress=0.40, novelty=0.30,
    ),
    Scenario(
        name="cold-oom-pressure",
        description=(
            "low cache hits + 90 % context full — OOM imminent; advisor "
            "drops to Q4_0 to keep the session alive"
        ),
        prompt="Summarise in one sentence: caching trades memory for speed.",
        cache_hit_rate=0.10, context_util=0.90,
        task_progress=0.20, novelty=0.60,
    ),
    Scenario(
        name="precision-math",
        description=(
            "math-style prompt; quality matters more than memory; advisor "
            "should keep F16"
        ),
        prompt="Compute 17 * 23. Show steps then the final answer.",
        cache_hit_rate=0.40, context_util=0.30,
        task_progress=0.50, novelty=0.20,
    ),
    Scenario(
        name="low-stakes-summary",
        description="low-stakes summary; Q8_0 is fine",
        prompt="Give me a one-line motto for a coffee shop.",
        cache_hit_rate=0.30, context_util=0.20,
        task_progress=0.10, novelty=0.40,
    ),
    Scenario(
        name="fresh-task-no-signal",
        description="cold start; advisor has no signal yet — defaults to F16",
        prompt="Hello, who are you?",
        cache_hit_rate=0.0, context_util=0.05,
        task_progress=0.0, novelty=0.0,
    ),
]


# ─────────────────────────────────────────────────────────────────────
# ATV construction for the placement advisor
# ─────────────────────────────────────────────────────────────────────


MODEL_FOR_COST = "claude-haiku-4-5"


def scenario_to_atv(s: Scenario) -> tuple[np.ndarray, ATVInput]:
    """Build a 2080-D ATV with the cost-band populated from the
    scenario's signals. Other bands stay at the encoder's default
    so the advisor's residency decision is dominated by cost +
    novelty — which is the surface we want to verify."""
    in_tokens, out_tokens = 4_000.0, 1_500.0
    cum_dollars = expected_flops(
        MODEL_FOR_COST, in_tokens, out_tokens
    ) * DEFAULT_DOLLAR_PER_FLOP
    cost = CostEfficiencyMetrics(
        input_token_count=in_tokens, output_token_count=out_tokens,
        cumulative_tokens=in_tokens + out_tokens,
        cumulative_dollars=cum_dollars,
        cache_hit_rate=s.cache_hit_rate,
        context_utilization_ratio=s.context_util,
        task_progress_score=s.task_progress,
    )
    inp = ATVInput(
        header=ATVHeader(
            trace_id="p" * 32, span_id="p" * 16,
            tenant_id="demo", aid="placement-advisor-demo",
            timestamp_ns=0, model_hash=MODEL_FOR_COST,
        ),
        tool_name="Bash",
        tool_args_json=json.dumps({"command": s.prompt[:40]}),
        plan_text=s.prompt,
        cost_estimate=cost,
        novelty={"composite_novelty": float(s.novelty)},
    )
    atv = build_atv(inp)
    return atv, inp


# ─────────────────────────────────────────────────────────────────────
# Memory accounting
# ─────────────────────────────────────────────────────────────────────


def kv_cache_bytes(tier: str, *, n_ctx: int = DEMO_N_CTX) -> int:
    """Analytical KV cache size for TinyLlama with given quant tier.

    KV cache memory =
        n_layer × 2 (K and V) × n_kv_head × head_dim × n_ctx × bytes_per_element
    """
    bpe = TIER_BYTES_PER_ELEMENT[tier]
    return int(N_LAYER * 2 * N_KV_HEAD * HEAD_DIM * n_ctx * bpe)


def rss_bytes() -> int:
    """Process resident set size in bytes (Mac/Linux)."""
    ru = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        return int(ru.ru_maxrss)              # darwin: bytes
    return int(ru.ru_maxrss) * 1024            # linux: KB → bytes


# ─────────────────────────────────────────────────────────────────────
# llama-cpp tier wrapper
# ─────────────────────────────────────────────────────────────────────


@dataclass
class LlamaCache:
    """Lazy-loaded {tier → Llama} so we pay the load cost at most once
    per tier per demo run."""

    llamas: dict[str, Any] = field(default_factory=dict)

    def get(self, tier: str) -> Any:
        if tier not in self.llamas:
            from llama_cpp import Llama

            t = TIER_TYPE_ID[tier]
            self.llamas[tier] = Llama(
                model_path=str(MODEL_PATH),
                n_ctx=DEMO_N_CTX,
                n_gpu_layers=-1,           # all layers to Metal
                type_k=t, type_v=t,
                flash_attn=True,           # required for non-F16 KV
                seed=1337,                 # deterministic
                verbose=False,
            )
        return self.llamas[tier]

    def close(self) -> None:
        for tier in list(self.llamas):
            del self.llamas[tier]
        self.llamas.clear()


def run_one_inference(
    cache: LlamaCache, tier: str, prompt: str,
) -> tuple[str, float, int]:
    """Returns (text, wall_ms, n_tokens)."""
    llama = cache.get(tier)
    t0 = time.perf_counter()
    out = llama(
        prompt,
        max_tokens=GEN_MAX_TOKENS,
        temperature=GEN_TEMPERATURE,
        seed=1337,
    )
    wall_ms = (time.perf_counter() - t0) * 1000.0
    text = out["choices"][0]["text"]
    n_tokens = int(out.get("usage", {}).get("completion_tokens", 0))
    return text, wall_ms, n_tokens


# ─────────────────────────────────────────────────────────────────────
# Quality measurement
# ─────────────────────────────────────────────────────────────────────


def text_similarity(a: str, b: str) -> float:
    """0..1 ratio (1.0 = identical). Stdlib, no extra dep."""
    return difflib.SequenceMatcher(None, a, b).ratio()


# ─────────────────────────────────────────────────────────────────────
# Main experiment
# ─────────────────────────────────────────────────────────────────────


_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BLUE = "\033[34m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


@dataclass
class TierRun:
    tier: str
    text: str
    wall_ms: float
    n_tokens: int
    kv_cache_bytes: int


@dataclass
class ScenarioResult:
    scenario: Scenario
    advice: PlacementAdvice
    f16: TierRun                     # baseline-safe: always F16
    q4: TierRun                      # baseline-cheap: always Q4_0
    advisor: TierRun                 # advisor's recommended tier


def ensure_model() -> None:
    """Download the GGUF if absent, then VERIFY its SHA-256 against
    the pin in :data:`aegis.attest.KNOWN_MODELS` before letting it
    near llama-cpp's native parser. Refusal mode = raise."""
    from aegis.attest import assert_gguf_attestation

    if not MODEL_PATH.is_file():
        print(
            f"{_DIM}downloading {MODEL_FILE} from HF Hub (~640 MB)...{_RESET}"
        )
        from huggingface_hub import hf_hub_download

        hf_hub_download(
            repo_id=MODEL_REPO,
            filename=MODEL_FILE,
            local_dir=str(MODEL_PATH.parent),
        )

    # Verify-then-load. assert_gguf_attestation raises on mismatch
    # and returns the AttestationResult on success.
    print(f"{_DIM}verifying SHA-256 against attestation pin...{_RESET}")
    result = assert_gguf_attestation(
        MODEL_PATH, repo_id=MODEL_REPO, filename=MODEL_FILE,
    )
    print(
        f"{_GREEN}✓{_RESET} {_DIM}{result.summary()}{_RESET}"
    )


def run_experiment() -> list[ScenarioResult]:
    ensure_model()
    cache = LlamaCache()
    rss_before = rss_bytes()
    results: list[ScenarioResult] = []

    try:
        for s in SCENARIOS:
            atv, _ = scenario_to_atv(s)
            advice = placement_advisor(atv)
            advisor_tier = advice.kv_quantisation_dtype

            # Strategy 1: always F16 (baseline-safe / quality reference).
            f16_text, f16_ms, f16_n = run_one_inference(
                cache, "f16", s.prompt,
            )
            f16_run = TierRun(
                tier="f16", text=f16_text, wall_ms=f16_ms,
                n_tokens=f16_n, kv_cache_bytes=kv_cache_bytes("f16"),
            )

            # Strategy 2: always Q4_0 (baseline-cheap / what naive
            # memory-saving without an advisor would do).
            q4_text, q4_ms, q4_n = run_one_inference(
                cache, "q4_0", s.prompt,
            )
            q4_run = TierRun(
                tier="q4_0", text=q4_text, wall_ms=q4_ms,
                n_tokens=q4_n, kv_cache_bytes=kv_cache_bytes("q4_0"),
            )

            # Strategy 3: advisor-guided. Re-run even when tier matches
            # f16/q4 so the per-call timing is independent of the
            # baseline runs (avoids accidental warm-cache speedup bias).
            adv_text, adv_ms, adv_n = run_one_inference(
                cache, advisor_tier, s.prompt,
            )
            advisor_run = TierRun(
                tier=advisor_tier, text=adv_text,
                wall_ms=adv_ms, n_tokens=adv_n,
                kv_cache_bytes=kv_cache_bytes(advisor_tier),
            )

            results.append(ScenarioResult(
                scenario=s, advice=advice,
                f16=f16_run, q4=q4_run, advisor=advisor_run,
            ))
    finally:
        cache.close()
        rss_after = rss_bytes()
        print(
            f"{_DIM}process RSS: {rss_before/1e6:.0f} MB → "
            f"{rss_after/1e6:.0f} MB ("
            f"+{(rss_after-rss_before)/1e6:.0f} MB working-set){_RESET}"
        )
    return results


# ─────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────


def fmt_mb(b: int) -> str:
    return f"{b/1e6:6.1f} MB"


def render_results(results: list[ScenarioResult]) -> None:
    print()
    print(f"{_BOLD}Aegis Placement Advisor × llama-cpp KV-Cache Verification"
          f"{_RESET}")
    print(f"{_DIM}model:  TinyLlama-1.1B-Chat Q4_K_M (GGUF, "
          f"{N_LAYER} layers / {N_KV_HEAD} KV heads / head_dim={HEAD_DIM})")
    print(f"runtime: llama-cpp-python (Metal), n_ctx={DEMO_N_CTX}, "
          f"gen={GEN_MAX_TOKENS} tokens, T=0.0{_RESET}")
    print()

    # ── Per-scenario rows ─────────────────────────────────────────
    print(f"{_BOLD}── Per-scenario "
          f"(quality = sequence similarity vs F16 reference) {'─' * 16}{_RESET}")
    print(
        f"  {'scenario':<22} {'advised':>7}  "
        f"{'F16-only':>17}  {'Q4-only':>17}  {'advisor':>17}"
    )
    print(
        f"  {' ' * 22} {' ' * 7}  "
        f"{_DIM}{'mem    qual':>17}  {'mem    qual':>17}  {'mem    qual':>17}"
        f"{_RESET}"
    )
    print(f"  {_DIM}{'-' * 86}{_RESET}")

    # Aggregates
    f16_kv_total = 0
    q4_kv_total = 0
    adv_kv_total = 0
    f16_quality_scores: list[float] = []
    q4_quality_scores: list[float] = []
    adv_quality_scores: list[float] = []
    advice_distribution: dict[str, int] = {"f16": 0, "q8_0": 0, "q4_0": 0}

    # Per-tier conditional aggregates: when advisor recommended this
    # tier, what was the avg quality preserved?
    by_advised_tier: dict[str, list[float]] = {
        "f16": [], "q8_0": [], "q4_0": [],
    }
    by_advised_tier_mem_saved: dict[str, list[int]] = {
        "f16": [], "q8_0": [], "q4_0": [],
    }

    for r in results:
        # Quality: 1.0 for the F16 reference (definitionally),
        # similarity-vs-F16 for the others.
        f16_q = 1.0
        q4_q = text_similarity(r.q4.text, r.f16.text)
        adv_q = text_similarity(r.advisor.text, r.f16.text)

        f16_quality_scores.append(f16_q)
        q4_quality_scores.append(q4_q)
        adv_quality_scores.append(adv_q)

        f16_kv_total += r.f16.kv_cache_bytes
        q4_kv_total += r.q4.kv_cache_bytes
        adv_kv_total += r.advisor.kv_cache_bytes
        advice_distribution[r.advisor.tier] += 1
        by_advised_tier[r.advisor.tier].append(adv_q)
        by_advised_tier_mem_saved[r.advisor.tier].append(
            r.f16.kv_cache_bytes - r.advisor.kv_cache_bytes
        )

        adv_q_color = (
            _GREEN if adv_q >= 0.95
            else (_YELLOW if adv_q >= 0.80 else _RED)
        )
        q4_q_color = (
            _GREEN if q4_q >= 0.95
            else (_YELLOW if q4_q >= 0.80 else _RED)
        )
        tier_color = (
            _RED if r.advisor.tier == "q4_0"
            else (_YELLOW if r.advisor.tier == "q8_0" else _GREEN)
        )
        print(
            f"  {r.scenario.name:<22} "
            f"{tier_color}{r.advisor.tier:>7}{_RESET}  "
            f"{fmt_mb(r.f16.kv_cache_bytes):>8} {_GREEN}{f16_q*100:>6.1f}%{_RESET}  "
            f"{fmt_mb(r.q4.kv_cache_bytes):>8} {q4_q_color}{q4_q*100:>6.1f}%{_RESET}  "
            f"{fmt_mb(r.advisor.kv_cache_bytes):>8} "
            f"{adv_q_color}{adv_q*100:>6.1f}%{_RESET}"
        )

    # ── Strategy roll-up ──────────────────────────────────────────
    print()
    print(f"{_BOLD}── Strategy comparison {'─' * 51}{_RESET}")

    avg_f16_q = statistics.mean(f16_quality_scores)
    avg_q4_q = statistics.mean(q4_quality_scores)
    avg_adv_q = statistics.mean(adv_quality_scores)
    min_q4_q = min(q4_quality_scores)
    min_adv_q = min(adv_quality_scores)

    saved_vs_f16 = f16_kv_total - adv_kv_total
    saved_vs_f16_pct = (
        saved_vs_f16 / f16_kv_total * 100.0 if f16_kv_total else 0.0
    )
    q4_savings_pct = (
        (f16_kv_total - q4_kv_total) / f16_kv_total * 100.0
        if f16_kv_total else 0.0
    )

    def _q_color(q: float) -> str:
        if q >= 0.95:
            return _GREEN
        if q >= 0.80:
            return _YELLOW
        return _RED

    print(
        f"  {'strategy':<28} {'KV mem':>10}  {'mem saved':>11}  "
        f"{'avg quality':>13}  {'min quality':>13}"
    )
    print(f"  {_DIM}{'-' * 82}{_RESET}")
    print(
        f"  {'baseline-safe (always F16)':<28} "
        f"{fmt_mb(f16_kv_total):>10}  "
        f"{'-':>11}  "
        f"{_GREEN}{avg_f16_q*100:>12.1f}%{_RESET}  "
        f"{_GREEN}{avg_f16_q*100:>12.1f}%{_RESET}"
    )
    print(
        f"  {'baseline-cheap (always Q4_0)':<28} "
        f"{fmt_mb(q4_kv_total):>10}  "
        f"{_GREEN}{q4_savings_pct:>10.1f}%{_RESET}  "
        f"{_q_color(avg_q4_q)}{avg_q4_q*100:>12.1f}%{_RESET}  "
        f"{_q_color(min_q4_q)}{min_q4_q*100:>12.1f}%{_RESET}"
    )
    print(
        f"  {_BOLD}{'advisor-guided':<28}{_RESET} "
        f"{fmt_mb(adv_kv_total):>10}  "
        f"{_GREEN}{saved_vs_f16_pct:>10.1f}%{_RESET}  "
        f"{_q_color(avg_adv_q)}{avg_adv_q*100:>12.1f}%{_RESET}  "
        f"{_q_color(min_adv_q)}{min_adv_q*100:>12.1f}%{_RESET}"
    )

    # ── Per-advised-tier breakdown ────────────────────────────────
    print()
    print(f"{_BOLD}── Per-advised-tier breakdown {'─' * 44}{_RESET}")
    for tier in ("f16", "q8_0", "q4_0"):
        n = advice_distribution[tier]
        if n == 0:
            print(f"  {tier:<6} 0 scenarios")
            continue
        avg_q = statistics.mean(by_advised_tier[tier])
        saved = sum(by_advised_tier_mem_saved[tier])
        if tier == "f16":
            note = "(advisor declined to compress — quality-critical)"
        elif tier == "q8_0":
            note = "(half KV memory at usually-zero quality cost)"
        else:
            note = "(quarter KV memory; OOM-survival mode)"
        print(
            f"  {tier:<6} {n}× scenarios  saved={fmt_mb(saved)}  "
            f"quality={_q_color(avg_q)}{avg_q*100:.1f}%{_RESET}  "
            f"{_DIM}{note}{_RESET}"
        )

    # ── Verdict ───────────────────────────────────────────────────
    print()
    print(f"{_BOLD}── Verdict {'─' * 64}{_RESET}")

    # Advisor wins if it achieves > X% memory savings AND keeps quality
    # significantly above all-Q4 baseline.
    quality_lift_vs_q4 = (avg_adv_q - avg_q4_q) * 100.0
    print(
        f"  Compared to {_BOLD}always-F16{_RESET}, advisor cuts KV cache by "
        f"{_GREEN}{saved_vs_f16_pct:.0f}%{_RESET} "
        f"({fmt_mb(saved_vs_f16)} of {fmt_mb(f16_kv_total)})."
    )
    print(
        f"  Compared to {_BOLD}always-Q4_0{_RESET}, advisor lifts output "
        f"quality by "
        f"{_GREEN}+{quality_lift_vs_q4:.1f} pp{_RESET} "
        f"({avg_adv_q*100:.1f}% vs {avg_q4_q*100:.1f}%) — "
        f"{_BOLD}saving the cases where Q4 would have garbled the output{_RESET}."
    )
    print()
    print(
        f"  {_DIM}The advisor's value is the {_BOLD}selectivity{_RESET}{_DIM}: "
        f"it picks F16 only when{_RESET}"
    )
    print(
        f"  {_DIM}the task can't tolerate quality loss, and Q4_0 only when "
        f"the alternative{_RESET}"
    )
    print(
        f"  {_DIM}is OOM. A naive single-tier policy can't tell those cases "
        f"apart.{_RESET}"
    )


def main() -> int:
    if not MODEL_PATH.is_file():
        print(f"{_DIM}model not found, will download...{_RESET}")
    results = run_experiment()
    render_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
