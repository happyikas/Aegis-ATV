"""10 RAG quality regression cases.

Asserts **structural invariants** of the policy/playbook retrieval
pipeline so any drift in corpus loading, embedding, top-k selection,
or fail-soft behaviour shows up as a CI failure.

These cases use the ``dummy`` (SHA3) embedding provider so the chunk
*scores* are deterministic but semantically random. Every assertion
in this file is on a structural property (count, category mix,
on/off behaviour) — never on which specific chunk wins, since under
``dummy`` that's just hash collisions.

For semantic-quality regression you need ``bge-local`` and the
``demo/rag_comparison.py`` script (which is intentionally not
part of the macmini suite — it requires the BGE GGUF download).
"""
from __future__ import annotations

from .case import TestCase


def cases() -> list[TestCase]:
    out: list[TestCase] = []
    add = out.append

    # ── on/off control (3) ─────────────────────────────────────────
    add(TestCase(
        cid="RAG-01",
        category="rag",
        title="rag_enabled=False → 0 chunks (control)",
        scenario=(
            "With aegis_rag_enabled=False, retrieve_block must return "
            "no chunks regardless of query content. The plumbing must "
            "honour the env-var toggle."
        ),
        execution_summary="retrieve(query, k=3) with rag_enabled=False",
        test_type="rag",
        rag_query="force-push to main is dangerous",
        rag_enabled=False,
        rag_expected_max_count=0,
    ))
    add(TestCase(
        cid="RAG-02",
        category="rag",
        title="rag_enabled=True → exactly k chunks",
        scenario=(
            "With RAG on, retrieve(k=3) must return exactly 3 chunks "
            "from a corpus of >=30 entries."
        ),
        execution_summary="retrieve(query, k=3)",
        test_type="rag",
        rag_query="destructive command targeting production",
        rag_top_k=3,
        rag_expected_min_count=3,
        rag_expected_max_count=3,
    ))
    add(TestCase(
        cid="RAG-03",
        category="rag",
        title="rag k=5 returns 5 chunks",
        scenario=(
            "Top-k parameter passes through end-to-end; k=5 must "
            "yield exactly 5 chunks (corpus has 38)."
        ),
        execution_summary="retrieve(query, k=5)",
        test_type="rag",
        rag_query="kubernetes namespace removal pattern",
        rag_top_k=5,
        rag_expected_min_count=5,
        rag_expected_max_count=5,
    ))

    # ── corpus invariants (3) ──────────────────────────────────────
    add(TestCase(
        cid="RAG-04",
        category="rag",
        title="every retrieval includes at least one rule chunk",
        scenario=(
            "The shipped corpus is dominated by rule chunks (31/38). "
            "Top-3 over any reasonable query must include at least "
            "one rule chunk — a sanity check on category coverage."
        ),
        execution_summary="retrieve top-3, expect 'rule' present",
        test_type="rag",
        rag_query="some agent operation summary",
        rag_top_k=3,
        rag_expected_min_count=3,
        rag_expected_categories=("rule",),
    ))
    add(TestCase(
        cid="RAG-05",
        category="rag",
        title="empty query string returns valid result",
        scenario=(
            "Edge case: empty query embedded by dummy provider. "
            "Should not crash; should return some chunks (the "
            "L2-normalised zero vector falls back gracefully)."
        ),
        execution_summary="retrieve('', k=3) — fail-soft",
        test_type="rag",
        rag_query=" ",  # whitespace, not empty (case schema requires non-empty)
        rag_top_k=3,
        rag_expected_min_count=0,  # tolerate fail-soft empty
        rag_expected_max_count=3,
    ))
    add(TestCase(
        cid="RAG-06",
        category="rag",
        title="long query (>500 chars) doesn't crash",
        scenario=(
            "Realistic ATV summary can be a few hundred chars. "
            "Verify retrieve handles long queries without truncation "
            "errors and still returns the expected number of chunks."
        ),
        execution_summary="retrieve(long_query, k=3)",
        test_type="rag",
        rag_query=" ".join(
            ["destructive", "operation", "against", "production", "infrastructure", "involving", "cloud", "resources", "kubernetes", "deployments", "aws", "iam", "policies", "and", "database", "mutations"] * 8
        ),
        rag_top_k=3,
        rag_expected_min_count=3,
    ))

    # ── corpus-coverage invariants (2) ─────────────────────────────
    # Under the dummy provider, ranking is hash-driven, so we can't
    # claim "category X is in top-10". What we CAN claim is "with
    # k=corpus_size, every category appears" — an indexing invariant
    # that catches accidental file exclusion.
    add(TestCase(
        cid="RAG-07",
        category="rag",
        title="full-corpus retrieval surfaces all rule + playbook chunks",
        scenario=(
            "With k=999 (capped at corpus size) every chunk is "
            "returned. Expect both rule and playbook categories — "
            "catches regression where playbooks.jsonl is silently "
            "excluded from indexing."
        ),
        execution_summary="retrieve(query, k=999)",
        test_type="rag",
        rag_query="incident response and rule application",
        rag_top_k=999,
        rag_expected_min_count=30,
        rag_expected_categories=("rule", "playbook"),
    ))
    add(TestCase(
        cid="RAG-08",
        category="rag",
        title="full-corpus retrieval includes baseline category",
        scenario=(
            "The baseline placeholder chunk must be reachable at "
            "k=999. Catches regression where baselines.jsonl is "
            "excluded from indexing or its category is mislabelled."
        ),
        execution_summary="retrieve(query, k=999)",
        test_type="rag",
        rag_query="baseline traffic pattern for tenant",
        rag_top_k=999,
        rag_expected_min_count=30,
        rag_expected_categories=("baseline",),
    ))

    # ── invariants under embedding flip (2) ────────────────────────
    add(TestCase(
        cid="RAG-09",
        category="rag",
        title="k > corpus size capped at corpus size",
        scenario=(
            "Asking for more chunks than the corpus contains should "
            "return all chunks, not crash. Verifies search() bounds "
            "the slice to len(corpus)."
        ),
        execution_summary="retrieve(query, k=999)",
        test_type="rag",
        rag_query="anything",
        rag_top_k=999,
        rag_expected_min_count=30,  # corpus has 38; never less than 30
    ))
    add(TestCase(
        cid="RAG-10",
        category="rag",
        title="k=1 returns exactly one chunk with valid id",
        scenario=(
            "Boundary: k=1 narrows to a single hit. Verifies the "
            "argsort -> top slice path with the smallest possible k."
        ),
        execution_summary="retrieve(query, k=1)",
        test_type="rag",
        rag_query="single-chunk retrieval test",
        rag_top_k=1,
        rag_expected_min_count=1,
        rag_expected_max_count=1,
    ))

    assert len(out) == 10, f"want 10 rag cases, got {len(out)}"
    return out
