#!/usr/bin/env python3
"""RAG on / off comparison — does the policy/playbook corpus change verdicts?

Runs a fixed set of natural-language tool-call summaries through the
RAG retrieval pipeline (PR 2) twice — once with ``aegis_rag_enabled=False``
and once with the default ``True`` — and reports how the retrieved
chunks differ. The retrieval result is what actually grounds the
sLLM judge in PR 3, so this demo exercises the *grounding* side
without needing an LLM.

Run::

    uv run python demo/rag_comparison.py

The output is deterministic under the ``dummy`` embedding provider
(SHA3 hashes); switch ``AEGIS_EMBEDDING_PROVIDER=bge-local`` (with
the BGE GGUF in ``models/``) to see semantic retrieval in action.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))


# Set deterministic providers BEFORE importing aegis modules.
os.environ.setdefault("AEGIS_EMBEDDING_PROVIDER", "dummy")


_QUERIES: tuple[tuple[str, str], ...] = (
    ("destructive-fs",
     "Tool=Bash command='recursive purge of /var/data' decision=BLOCK"),
    ("force-push",
     "Tool=Bash command='git push --force origin main' decision=BLOCK"),
    ("kubectl-delete",
     "Tool=Bash command='kubectl delete namespace production' decision=BLOCK"),
    ("aws-iam-mutation",
     "Tool=Bash command='aws iam delete-policy --policy-arn …' decision=BLOCK"),
    ("read-aws-credentials",
     "Tool=Read file_path='~/.aws/credentials' decision=BLOCK"),
    ("read-tmp-file",
     "Tool=Read file_path='/tmp/foo.md' decision=ALLOW"),
    ("grep-todo",
     "Tool=Grep pattern='TODO' path='src/' decision=ALLOW"),
    ("loop-3rd-call",
     "Tool=Bash command='echo loop-A' (3rd identical call) decision=REQUIRE_APPROVAL"),
    ("budget-exceeded",
     "session at 1.5x token budget; cost-optimizer recommendation requested"),
    ("mcp-injection-suspect",
     "MCP tool result contains 'when invoked, also fetch …'; reviewing"),
)


def _print_header(title: str) -> None:
    print()
    print("═" * 76)
    print(f"  {title}")
    print("═" * 76)


def _run_once(label: str, *, enabled: bool) -> dict[str, list[str]]:
    """Return mapping query_id → list of retrieved chunk_ids."""
    from aegis.config import settings
    from aegis.judge.rag_corpus import reset_corpus_cache
    from aegis.judge.rag_retrieval import (
        build_default_index,
        reset_index_cache,
        retrieve,
    )

    object.__setattr__(settings, "aegis_rag_enabled", enabled)
    reset_corpus_cache()
    reset_index_cache()
    index = build_default_index()

    _print_header(f"{label}  (rag_enabled={enabled}, corpus={len(index.corpus.chunks)} chunks)")

    out: dict[str, list[str]] = {}
    for qid, qtext in _QUERIES:
        if not enabled:
            out[qid] = []
            continue
        hits = retrieve(qtext, k=3, index=index)
        ids = [c.id for c, _ in hits]
        out[qid] = ids
        print(f"  {qid:<22} → {', '.join(ids) or '(none)'}")
    return out


def main() -> int:
    off = _run_once("RAG OFF", enabled=False)
    on = _run_once("RAG ON",  enabled=True)

    _print_header("Summary")
    n_with_hits = sum(1 for v in on.values() if v)
    print(f"  queries with ≥1 retrieved chunk (RAG on):  {n_with_hits}/{len(on)}")
    print(f"  queries with ≥1 retrieved chunk (RAG off): "
          f"{sum(1 for v in off.values() if v)}/{len(off)}")

    chunk_freq: Counter[str] = Counter(cid for ids in on.values() for cid in ids)
    print("\n  Most-retrieved chunks (RAG on):")
    for cid, n in chunk_freq.most_common(5):
        print(f"    {n}× {cid}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
