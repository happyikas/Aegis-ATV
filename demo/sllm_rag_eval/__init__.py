"""30-case RAG + sLLM benchmark suite.

Self-contained benchmark for the v3.0 RAG-grounded judge stack
(PRs #87/#88/#89). Drives 30 fixed natural-language tool-call
summaries through 4 configurations and reports per-case decisions
+ aggregate accuracy.

Configurations:

* ``dummy-norag``  — DummyJudge, no corpus retrieval (baseline)
* ``dummy-rag``    — DummyJudge, corpus retrieval enabled
* ``sllm-norag``   — LocalPhiJudge, no corpus retrieval
* ``sllm-rag``     — LocalPhiJudge, corpus retrieval enabled

Run:

    uv run python -m demo.sllm_rag_eval [--mode all] [--limit N]

The script auto-detects which configurations are runnable in the
current environment (skips ``sllm-*`` if the GGUF is missing,
``haiku-*`` if no API key, ``*-rag`` always works because the
corpus retrieval has a deterministic ``dummy`` fallback).
"""
from __future__ import annotations

__version__ = "1.0.0"
