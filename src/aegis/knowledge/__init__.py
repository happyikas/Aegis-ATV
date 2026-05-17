"""Aegis ContextMemory knowledge layer (v0.5.15).

A derived, wiki-shaped knowledge base built on top of the raw
ContextMemory JSONL store. The raw store keeps audit + replay
responsibility; this layer is the **sLLM-ready** view that
advisors consume to give workflow advice.

Layout
======

::

    ~/.aegis/context_memory.jsonl     ← raw events (unchanged)
    ~/.aegis/knowledge/               ← derived wiki (NEW)
      index.json                      ← catalog
      agent_foo.json                  ← per-entity entries
      tool_Bash.json
      pattern_loop_Bash.json
      ...

Each entry is a self-contained markdown-style "wiki article"
with:

* **Lead summary** — 1-2 sentences, always shown.
* **Infobox** — structured key-value facts (LLMs parse these
  most reliably).
* **Sections** — ordered markdown subsections.
* **Cross-references** — explicit ``related`` entry_ids.
* **Tags + confidence + n_observations** — provenance metadata.

Public surface
==============

Schema
------
* :class:`KnowledgeEntry` — one wiki article.
* :class:`InfoBox`, :class:`Section`, :class:`EntryKind` — building blocks.

Builder
-------
* :func:`build_knowledge` — derive entries from a list of
  ContextMemory records.

Storage
-------
* :func:`save_entry`, :func:`load_entry` — per-entry I/O.
* :func:`save_index`, :func:`load_index` — catalog I/O.
* :func:`knowledge_dir`, :func:`entry_path`, :func:`index_path`
  — path resolution (honours ``AEGIS_KNOWLEDGE_DIR``).

Renderer
--------
* :func:`render_entry_markdown` — one entry as markdown.
* :func:`render_advisor_context` — multi-entry prompt composition
  for sLLM advisors.

Retrieval
---------
* :func:`get_entry`, :func:`get_entries_by_id` — by-ID lookup.
* :func:`get_entries_for_agent` — agent + cross-refs (default
  pre-pop for advisor prompts).
* :func:`search_by_kind_or_tag` — catalog enumeration.

Build cadence
=============

The knowledge layer is **batch-built**, not streamed. Operators
re-run ``aegis knowledge build`` after every burn-in window to
refresh the wiki. Same explicit-rebuild contract as the
autonomy trust table — preserves the audit property that "the
advice in this window comes from this exact knowledge snapshot".

A future v0.6 PR can wire the sLLM advisor (TripleAxisAdvisor /
ActionAdvice) to read the wiki via :func:`get_entries_for_agent`
+ :func:`render_advisor_context`; v0.5.15 ships the layer alone
so it can be reviewed in isolation."""

from aegis.knowledge.advisor import (
    ContextMetrics,
    advisor_knowledge_enabled,
    clear_advisor_cache,
    knowledge_context_for_advisor,
    measure_context,
)
from aegis.knowledge.builder import (
    CONFIDENCE_FULL_AT,
    TOP_K_AGENTS_PER_PATTERN,
    TOP_K_PATTERNS_PER_TOOL,
    TOP_K_TOOLS_PER_AGENT,
    build_knowledge,
)
from aegis.knowledge.render import (
    render_advisor_context,
    render_entry_markdown,
)
from aegis.knowledge.retrieve import (
    get_entries_by_id,
    get_entries_for_agent,
    get_entry,
    search_by_kind_or_tag,
)
from aegis.knowledge.schema import (
    SCHEMA_VERSION,
    EntryKind,
    InfoBox,
    KnowledgeEntry,
    Section,
    make_entry_id,
    split_entry_id,
)
from aegis.knowledge.store import (
    IndexEntry,
    entry_path,
    index_metadata,
    index_path,
    knowledge_dir,
    load_entry,
    load_index,
    save_entry,
    save_index,
)

__all__ = [
    "CONFIDENCE_FULL_AT",
    "ContextMetrics",
    "EntryKind",
    "IndexEntry",
    "InfoBox",
    "KnowledgeEntry",
    "SCHEMA_VERSION",
    "Section",
    "TOP_K_AGENTS_PER_PATTERN",
    "TOP_K_PATTERNS_PER_TOOL",
    "TOP_K_TOOLS_PER_AGENT",
    "advisor_knowledge_enabled",
    "build_knowledge",
    "clear_advisor_cache",
    "entry_path",
    "get_entries_by_id",
    "get_entries_for_agent",
    "get_entry",
    "index_metadata",
    "index_path",
    "knowledge_context_for_advisor",
    "knowledge_dir",
    "load_entry",
    "load_index",
    "make_entry_id",
    "measure_context",
    "render_advisor_context",
    "render_entry_markdown",
    "save_entry",
    "save_index",
    "search_by_kind_or_tag",
    "split_entry_id",
]
