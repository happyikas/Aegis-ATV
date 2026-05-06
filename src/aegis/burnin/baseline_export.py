"""Per-tenant baseline exporter — turns audit JSONL into a RAG chunk.

The shipped ``policies/rag_corpus/baselines.jsonl`` contains a generic
placeholder. Real per-tenant baselines are produced by walking the
local audit log (``~/.aegis/audit.jsonl``) and summarising the
operator's typical traffic in natural language.

Privacy contract
----------------

The audit log only stores **tool input keys** (e.g.
``["file_path", "old_string", "new_string"]``), never the values. So
the baseline describes patterns at the metadata level — which tools
are used, how often, with which keys — and never references paths,
commands, or session content.

Public surface
--------------

* :func:`analyse_audit` — pure function: audit path → ``BaselineSummary``.
* :func:`render_baseline_chunk` — ``BaselineSummary`` → RAG chunk dict.
* :func:`export_to_corpus` — full pipeline that overwrites the
  ``baselines.jsonl`` of the configured RAG corpus directory.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_AUDIT = Path.home() / ".aegis" / "audit.jsonl"
_TOP_TOOLS = 5
_TOP_KEYS = 5
_MIN_RECORDS_FOR_USEFUL_BASELINE = 10


@dataclass
class BaselineSummary:
    tenant: str
    n_records: int
    n_pretool: int
    n_posttool: int
    n_sessions: int
    earliest_ts_ns: int
    latest_ts_ns: int
    tool_freq: tuple[tuple[str, int], ...] = ()
    tool_input_keys: tuple[tuple[str, int], ...] = ()
    decisions: dict[str, int] = field(default_factory=dict)
    avg_calls_per_session: float = 0.0
    earliest_iso: str = ""
    latest_iso: str = ""

    @property
    def is_useful(self) -> bool:
        """Below this threshold the summary degenerates to noise."""
        return self.n_records >= _MIN_RECORDS_FOR_USEFUL_BASELINE


def _iter_records(audit_path: Path) -> Iterable[dict[str, Any]]:
    if not audit_path.is_file():
        return
    for raw in audit_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _iso(ts_ns: int) -> str:
    if ts_ns <= 0:
        return ""
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ns / 1_000_000_000)
    )


def analyse_audit(
    audit_path: Path | None = None,
    *,
    tenant: str = "local",
) -> BaselineSummary:
    """Walk the audit log and return summary stats. Pure / deterministic."""
    path = audit_path if audit_path is not None else _DEFAULT_AUDIT

    n_total = 0
    n_pre = 0
    n_post = 0
    aids: set[str] = set()
    tool_counter: Counter[str] = Counter()
    key_counter: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    earliest = 0
    latest = 0

    for rec in _iter_records(path):
        n_total += 1
        hook = rec.get("hook", "")
        if hook == "PreToolUse":
            n_pre += 1
        elif hook == "PostToolUse":
            n_post += 1
        if (aid := rec.get("aid")):
            aids.add(str(aid))
        if (tool := rec.get("tool")):
            tool_counter[str(tool)] += 1
        keys = rec.get("tool_input_keys") or []
        if isinstance(keys, list):
            for k in keys:
                if isinstance(k, str):
                    key_counter[k] += 1
        if (decision := rec.get("decision")):
            decisions[str(decision)] += 1
        ts = int(rec.get("ts_ns") or 0)
        if ts > 0:
            earliest = ts if earliest == 0 else min(earliest, ts)
            latest = max(latest, ts)

    n_sessions = len(aids)
    avg_per_session = (n_total / n_sessions) if n_sessions else 0.0

    return BaselineSummary(
        tenant=tenant,
        n_records=n_total,
        n_pretool=n_pre,
        n_posttool=n_post,
        n_sessions=n_sessions,
        earliest_ts_ns=earliest,
        latest_ts_ns=latest,
        tool_freq=tuple(tool_counter.most_common(_TOP_TOOLS)),
        tool_input_keys=tuple(key_counter.most_common(_TOP_KEYS)),
        decisions=dict(decisions),
        avg_calls_per_session=avg_per_session,
        earliest_iso=_iso(earliest),
        latest_iso=_iso(latest),
    )


def _format_pairs(pairs: tuple[tuple[str, int], ...]) -> str:
    if not pairs:
        return "(none)"
    return ", ".join(f"{name} ({count})" for name, count in pairs)


def render_baseline_chunk(
    summary: BaselineSummary,
    *,
    chunk_id: str | None = None,
    created_at: str | None = None,
    valid_from: str | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    """Turn a ``BaselineSummary`` into the JSONL chunk shape consumed by
    ``aegis.judge.rag_corpus``.

    ``chunk_id`` defaults to ``baseline-{tenant}`` (legacy single-chunk
    layout). When the caller is rotating (PR ②) it should supply a
    date-suffixed id like ``baseline-{tenant}-{YYYYMMDD}`` so multiple
    snapshots can coexist with chained ``supersedes`` references.

    ``created_at`` / ``valid_from`` / ``supersedes`` are passed straight
    through to the chunk dict so the time-decay reranker (PR ③) and the
    validity filter (PR ②/temporal-RAG) can see this baseline as part
    of their time-aware retrieval."""
    cid = chunk_id if chunk_id is not None else f"baseline-{summary.tenant}"
    if not summary.is_useful:
        content = (
            f"Tenant '{summary.tenant}' has only {summary.n_records} "
            "audit records — too few to extract a meaningful traffic "
            "pattern. Treat any operation as fresh-context and rely on "
            "the rule + playbook chunks for grounding."
        )
    else:
        tools_str = _format_pairs(summary.tool_freq)
        keys_str = _format_pairs(summary.tool_input_keys)
        decisions_str = (
            ", ".join(f"{k}={v}" for k, v in sorted(summary.decisions.items()))
            or "(no PreToolUse decisions logged)"
        )
        content = (
            f"Tenant '{summary.tenant}' baseline derived from "
            f"{summary.n_records} audit records across "
            f"{summary.n_sessions} session(s) "
            f"({summary.earliest_iso} to {summary.latest_iso}). "
            f"Most-used tools: {tools_str}. "
            f"Most-used tool input keys: {keys_str}. "
            f"Decision distribution: {decisions_str}. "
            f"Average tool calls per session: "
            f"{summary.avg_calls_per_session:.1f}. "
            "Treat traffic that diverges sharply from this profile "
            "(unfamiliar tools, unusual key combinations, decision "
            "mix shifts) as a signal worth flagging."
        )
    out: dict[str, Any] = {
        "id": cid,
        "category": "baseline",
        "title": f"Tenant {summary.tenant} traffic baseline",
        "content": content,
        "tags": ["baseline", summary.tenant],
        "policy_rule": None,
        "decision": "ALLOW",
    }
    if created_at is not None:
        out["created_at"] = created_at
    if valid_from is not None:
        out["valid_from"] = valid_from
    if supersedes is not None:
        out["supersedes"] = supersedes
    return out


def _now_iso() -> str:
    """Stable ISO 8601 UTC stamp for chunk timestamps."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _rotated_chunk_id(tenant: str, when_iso: str) -> str:
    """``baseline-<tenant>-<YYYYMMDD>`` — datestamped slug for
    multi-snapshot layouts. When a tenant rotates more than once a
    day, the second call would collide; the rotate path detects that
    and falls through to overwrite-mode for the same-day case."""
    date = when_iso[:10].replace("-", "")
    return f"baseline-{tenant}-{date}"


def _read_existing_baselines(path: Path) -> list[dict[str, Any]]:
    """Return parsed JSON objects from ``baselines.jsonl``; tolerates
    the file being absent or empty."""
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _seal_previous_for_tenant(
    existing: list[dict[str, Any]], tenant: str, *, until_iso: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Find the still-open baseline for ``tenant`` (no ``valid_until``)
    and stamp ``valid_until=until_iso`` on it. Returns
    ``(sealed_chunk, full_list)``; returns ``(None, existing)`` when
    there is nothing to seal (placeholder template, other tenants,
    or already-rotated chunks)."""
    sealed: dict[str, Any] | None = None
    out = []
    for chunk in existing:
        is_match = (
            chunk.get("category") == "baseline"
            and tenant in (chunk.get("tags") or [])
            and chunk.get("valid_until") is None
        )
        if is_match and sealed is None:
            new = dict(chunk)
            new["valid_until"] = until_iso
            sealed = new
            out.append(new)
        else:
            out.append(chunk)
    return sealed, out


def export_to_corpus(
    audit_path: Path | None = None,
    *,
    tenant: str = "local",
    corpus_dir: Path | None = None,
    rotate: bool = False,
) -> tuple[Path, BaselineSummary]:
    """Run the full pipeline: analyse → render → write baselines.jsonl.

    Behaviour split:

    * ``rotate=False`` (default, back-compat with PR #90) — overwrites
      the entire ``baselines.jsonl`` with a single chunk for the named
      tenant. Lower-friction for one-tenant local installs.

    * ``rotate=True`` (PR ②) — appends a new datestamped chunk and
      stamps ``valid_until=now`` on the previous open baseline for the
      same tenant. The ``supersedes`` link makes the chain traversable
      for audit replay; the validity-window filter (PR #94) + time
      decay (PR ③) make sure retrieval at any anchor time picks the
      right snapshot. Other tenants' baselines are left untouched, so
      multi-tenant Sidecar deployments can call this once per tenant
      on a schedule and accumulate per-tenant histories.

    Returns the ``(path_written, summary)`` pair.
    """
    summary = analyse_audit(audit_path, tenant=tenant)

    if corpus_dir is None:
        from aegis.judge.rag_corpus import default_corpus_dir
        corpus_dir = default_corpus_dir()
    corpus_dir.mkdir(parents=True, exist_ok=True)
    out_path = corpus_dir / "baselines.jsonl"

    if rotate:
        now_iso = _now_iso()
        existing = _read_existing_baselines(out_path)
        sealed, existing_after_seal = _seal_previous_for_tenant(
            existing, tenant, until_iso=now_iso,
        )
        new_id = _rotated_chunk_id(tenant, now_iso)
        # Same-day collision: drop the colliding entry from existing
        # (it was a previous rotation we're replacing). The ``sealed``
        # entry will still carry valid_until even if its id collides,
        # but here it would be the new_id we're about to write — so
        # drop it.
        existing_after_seal = [
            c for c in existing_after_seal if c.get("id") != new_id
        ]
        new_chunk = render_baseline_chunk(
            summary,
            chunk_id=new_id,
            created_at=now_iso,
            valid_from=now_iso,
            supersedes=(sealed.get("id") if sealed else None),
        )
        with out_path.open("w", encoding="utf-8") as f:
            for c in existing_after_seal:
                f.write(json.dumps(c) + "\n")
            f.write(json.dumps(new_chunk) + "\n")
    else:
        chunk = render_baseline_chunk(summary)
        out_path.write_text(json.dumps(chunk) + "\n", encoding="utf-8")

    # Invalidate caches so the next retrieval sees the new content.
    from aegis.judge.rag_corpus import reset_corpus_cache
    from aegis.judge.rag_retrieval import reset_index_cache
    reset_corpus_cache()
    reset_index_cache()

    return out_path, summary


def render_export_report(summary: BaselineSummary, out_path: Path) -> str:
    """Human-readable progress text for the CLI."""
    lines = [
        f"[burnin export-baseline] tenant={summary.tenant}",
        f"  records:           {summary.n_records:,}",
        f"    PreToolUse:      {summary.n_pretool:,}",
        f"    PostToolUse:     {summary.n_posttool:,}",
        f"  distinct sessions: {summary.n_sessions:,}",
        f"  avg per session:   {summary.avg_calls_per_session:.1f}",
    ]
    if summary.tool_freq:
        lines.append(f"  top tools:         {_format_pairs(summary.tool_freq)}")
    if summary.tool_input_keys:
        lines.append(f"  top keys:          {_format_pairs(summary.tool_input_keys)}")
    if summary.earliest_iso and summary.latest_iso:
        lines.append(
            f"  time range:        {summary.earliest_iso} → {summary.latest_iso}"
        )
    if not summary.is_useful:
        lines.append(
            f"  ⚠ only {summary.n_records} records — chunk written but "
            "not actionable yet"
        )
    lines.append(f"  written to:        {out_path}")
    return "\n".join(lines)


__all__ = (
    "BaselineSummary",
    "analyse_audit",
    "render_baseline_chunk",
    "export_to_corpus",
    "render_export_report",
)
