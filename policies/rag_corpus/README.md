# Aegis RAG Corpus

External knowledge base used by the local-sLLM judge (and optionally the
Haiku judge) to ground its verdicts. The corpus is plain JSONL — one
chunk per line — so a tenant can add, remove, or override entries with
a one-line edit and zero retraining.

## Three corpus files

| File | Purpose | Typical size |
|------|---------|--------------|
| `rules.jsonl`     | Step311 / step310 / step320 rule descriptions in natural language. The judge sees *why* a regex fires, not just that it matched. | 20–40 chunks |
| `playbooks.jsonl` | Past-incident playbooks. Each entry is a "given this signal pattern, here is what we learned" note. | 3–20 chunks |
| `baselines.jsonl` | Tenant-specific baseline behavior templates. Filled in by Burn-in (`aegis burnin export-baseline` — future PR). Empty in shipped repo. | 1 per tenant |

## Chunk schema

Every line is one JSON object:

```json
{
  "id": "rule-fs-destructive",
  "category": "rule",
  "title": "재귀 파일 삭제 (rm -rf) 차단",
  "content": "재귀적으로 디렉터리 트리를 삭제하는 명령은 ...",
  "tags": ["filesystem", "destructive", "fs"],
  "policy_rule": "rule:fs_destructive",
  "decision": "BLOCK"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Stable identifier across releases (used by retrieval cache key). |
| `category` | enum | yes | `rule` / `playbook` / `baseline`. |
| `title` | string | yes | Short heading shown to the model. |
| `content` | string | yes | The body that gets retrieved + embedded. |
| `tags` | string[] | no | Extra keywords for keyword pre-filter (future). |
| `policy_rule` | string | no | Cross-reference to the step311 / step320 rule code. |
| `decision` | enum | no | `BLOCK` / `REQUIRE_APPROVAL` / `ALLOW` — the verdict this chunk argues for. |
| `valid_from` | string | no | ISO 8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`) — chunk first becomes effective at this time. Inclusive. Absent → always valid. |
| `valid_until` | string | no | ISO 8601 UTC — chunk stops being effective at this time. **Exclusive**. Absent → no end. |
| `supersedes` | string | no | ID of the chunk this entry replaces (informational; the validity window is what actually filters retrieval). |

### Validity windows (PR #94)

Without timestamps, RAG cannot tell stale rules apart from current ones. Each chunk can carry an optional validity window so that:

* **Forensic replay (PR #95)** can ask "what was the policy at the time of incident I-127?" and retrieval will return only the chunks that were in effect at that timestamp.
* **Rule supersession** is automatic — when a regex changes, the new chunk gets `valid_from: <today>` and the old chunk gets `valid_until: <today>` and `supersedes: <old-id>`. Retrieval at *current* time only sees the new one; retrieval at a pre-change timestamp still sees the old one.
* **Tenant baselines** become time-aware — last week's baseline gets `valid_from: <last-week>`; the previous month's baseline rolls off naturally without manual deletion.

Example superseded pair:

```json
{"id": "rule-aws-iam-mutation-v0", "category": "rule",
 "title": "AWS IAM mutation (v0)", "content": "...",
 "valid_from": "2024-01-01T00:00:00Z",
 "valid_until": "2024-08-01T00:00:00Z"}

{"id": "rule-aws-iam-mutation", "category": "rule",
 "title": "AWS IAM mutation", "content": "...",
 "valid_from": "2024-08-01T00:00:00Z",
 "supersedes": "rule-aws-iam-mutation-v0"}
```

## Loader

```python
from aegis.judge.rag_corpus import load_default_corpus

corpus = load_default_corpus()
print(len(corpus.chunks))                     # all chunks, all eras
print(corpus.chunks[0].title)

# Time-anchored view (PR ①):
import time
now = corpus.valid_at(time.time_ns())          # chunks effective right now
historical = corpus.valid_at(1_700_000_000_000_000_000)  # chunks at 2023-11-14
```

The loader is **stdlib-only** — no embedding, no model load. PR 2 added
the embedding-based retrieval; PR ① added validity-window filtering;
PR ② will thread an `anchor_ts_ns` through `retrieve()` so the
retrieval-time filter matches.

## Adding a new chunk

1. Pick the right file (`rules` for a regex rule, `playbooks` for an
   incident, `baselines` for tenant traffic patterns).
2. Append one JSON line. `id` must be unique across **all three files**.
3. Keep `content` under ~600 characters — the judge's context budget
   is small (Llama-3.2-1B has 8K, but RAG block is allotted ~2K).
4. Re-run `uv run pytest tests/unit/test_rag_corpus.py` to validate the
   schema.

The corpus is intentionally human-curated. Auto-generation from the
audit log is *out of scope* — that is what `case_memory` (BGE-vector
search over past ATVs) already does.

## Relationship to existing case memory

`src/aegis/judge/case_memory.py` is **case-based RAG**: BGE embeddings
of past ATV vectors retrieved via cosine similarity. It answers "what
happened last time something like this came in?".

This corpus is **policy / playbook RAG**: human-written explanations
retrieved by query semantic similarity. It answers "what does Aegis
think about this kind of operation, and what do other operators
recommend?".

Both can be active simultaneously. PR 2 wires the new path into
`_build_rag_block` alongside the existing case-memory path.
