"""Aegis-aware runtime integrations (v3.3+).

Thin adapters that wire a local LLM serving runtime (MLX-LM,
llama.cpp, vLLM, SGLang) to the Aegis advisory + closed-loop
endpoints. Each adapter is intentionally small — the runtime
keeps its own scheduler/memory layer; we only feed it hints.
"""
