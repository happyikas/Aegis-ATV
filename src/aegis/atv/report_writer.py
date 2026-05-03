"""ATV scenario report writer.

After each scenario runs through the firewall, we want a single
artefact the user can read to understand what happened. This module
takes (ATVInput, ATV vector, Verdict, optional context) and emits
both:

* **Markdown** report (human-readable) — saved to a path the user
  can open in any text editor or browser.
* **JSON** report (machine-readable) — for diff / replay / future
  CI gating.

The report intentionally exposes *every* observable surface of the
firewall pass:
- ATV header
- Per-subfield non-zero status (which of 30 subfields fired)
- Verdict (decision + reason + latency)
- M13 attribution top contributors (when present)
- Firewall step traces (all 13 steps)
- Sample raw values for high-signal subfields

This is the user-facing companion to ``demo/plugin_scenarios.py`` —
the script runs the scenario, this writer saves the proof.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from aegis.schema import ALL_SUBFIELDS, ATVInput, Verdict

_REPORT_VERSION = "atv-report-v1"


@dataclass
class ScenarioReport:
    """Full self-describing record of one scenario execution."""

    schema_version: str
    scenario_id: int | str
    title: str
    real_incident: str
    generated_at: str  # ISO-8601 local
    generated_at_ns: int

    # Input
    tool_name: str
    tool_args_json: str
    tenant_id: str
    aid: str
    trace_id: str
    span_id: str

    # Verdict
    decision: str
    reason: str
    expected_decision: list[str]
    pass_fail: str  # PASS | PARTIAL | FAIL

    # ATV summary
    atv_dim: int
    atv_sha3: str
    subfield_coverage: list[dict[str, Any]] = field(default_factory=list)
    n_subfields_total: int = 30
    n_subfields_nonzero: int = 0

    # M13 attribution (top 5)
    m13_top_contributors: list[dict[str, Any]] = field(default_factory=list)

    # Firewall step traces
    step_traces: dict[str, str] = field(default_factory=dict)
    n_steps: int = 0
    n_steps_blocking: int = 0  # steps that emitted a non-empty verdict

    # Latency
    latency_ms: float = 0.0

    # Misc
    extras: dict[str, Any] = field(default_factory=dict)


def _summarise_subfields(atv: np.ndarray) -> tuple[list[dict[str, Any]], int]:
    """Per-subfield non-zero status + top-3 magnitude values."""
    rows: list[dict[str, Any]] = []
    nz_count = 0
    for name, sl in ALL_SUBFIELDS:
        slc = atv[sl]
        nonzero = (slc != 0).any()
        max_val = float(np.abs(slc).max()) if slc.size > 0 else 0.0
        if nonzero:
            nz_count += 1
        rows.append({
            "subfield": name,
            "slice_start": sl.start,
            "slice_stop": sl.stop,
            "non_zero": bool(nonzero),
            "max_abs": round(max_val, 6),
        })
    return rows, nz_count


def _m13_top_contributors(
    verdict: Verdict, k: int = 5,
) -> list[dict[str, Any]]:
    """Pull top-K M13 attribution contributors when the verdict carries them."""
    # M13 attribution is surfaced via step_traces or via judge.subfield_attribution
    # in the JudgeVerdict (which step340 then encodes into the Verdict's reason +
    # step_traces). We look for `attribution=...` patterns in step traces.
    contributors: list[dict[str, Any]] = []
    for trace in verdict.step_traces.values():
        if not isinstance(trace, str) or "attribution=" not in trace:
            continue
        # Format: "...attribution=tool_arg_inspection:0.30,action_blast_radius:0.21..."
        try:
            attr_segment = trace.split("attribution=", 1)[1]
            # Stop at next " " or end of string
            attr_segment = attr_segment.split(" ", 1)[0]
            for pair in attr_segment.split(","):
                if ":" not in pair:
                    continue
                name, val = pair.split(":", 1)
                contributors.append({
                    "subfield": name.strip(),
                    "weight": float(val),
                })
        except (ValueError, IndexError):
            continue
        break  # first trace with attribution wins
    contributors.sort(key=lambda c: c["weight"], reverse=True)
    return contributors[:k]


def build_report(
    *,
    scenario_id: int | str,
    title: str,
    real_incident: str,
    inp: ATVInput,
    atv: np.ndarray,
    verdict: Verdict,
    expected_decision: list[str] | set[str],
    pass_fail: str,
    latency_ms: float = 0.0,
    extras: dict[str, Any] | None = None,
) -> ScenarioReport:
    """Assemble a :class:`ScenarioReport` from the firewall outputs."""
    rows, nz_count = _summarise_subfields(atv)
    contributors = _m13_top_contributors(verdict)
    n_blocking = sum(
        1 for v in verdict.step_traces.values()
        if isinstance(v, str) and any(
            kw in v for kw in ("BLOCK", "REQUIRE_APPROVAL")
        )
    )
    return ScenarioReport(
        schema_version=_REPORT_VERSION,
        scenario_id=scenario_id,
        title=title,
        real_incident=real_incident,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S %z"),
        generated_at_ns=time.time_ns(),
        tool_name=inp.tool_name,
        tool_args_json=inp.tool_args_json,
        tenant_id=inp.header.tenant_id,
        aid=inp.header.aid,
        trace_id=inp.header.trace_id,
        span_id=inp.header.span_id,
        decision=verdict.decision,
        reason=verdict.reason,
        expected_decision=sorted(expected_decision),
        pass_fail=pass_fail,
        atv_dim=int(atv.shape[0]),
        atv_sha3=hashlib.sha3_256(atv.tobytes()).hexdigest(),
        subfield_coverage=rows,
        n_subfields_total=len(rows),
        n_subfields_nonzero=nz_count,
        m13_top_contributors=contributors,
        step_traces=dict(verdict.step_traces),
        n_steps=len(verdict.step_traces),
        n_steps_blocking=n_blocking,
        latency_ms=round(latency_ms, 3),
        extras=extras or {},
    )


def to_markdown(r: ScenarioReport) -> str:
    """Render the report as a human-readable Markdown document."""
    badge = {"PASS": "✅", "PARTIAL": "🟡", "FAIL": "❌"}.get(r.pass_fail, "?")
    lines: list[str] = []
    lines.append(f"# {badge} Scenario {r.scenario_id} — {r.title}")
    lines.append("")
    lines.append(f"**Generated:** {r.generated_at}")
    lines.append(f"**Real-world incident:** {r.real_incident}")
    lines.append("")
    lines.append("## 1. Tool invocation")
    lines.append("")
    lines.append(f"- **Tool:** `{r.tool_name}`")
    lines.append(f"- **Args:** `{r.tool_args_json[:200]}{'…' if len(r.tool_args_json) > 200 else ''}`")
    lines.append(f"- **Tenant:** `{r.tenant_id}`")
    lines.append(f"- **Agent ID:** `{r.aid}`")
    lines.append(f"- **Trace ID:** `{r.trace_id[:16]}…`")
    lines.append("")
    lines.append("## 2. Verdict")
    lines.append("")
    lines.append(f"- **Decision:** **{r.decision}** (expected: {r.expected_decision})")
    lines.append(f"- **Pass/Fail:** {badge} **{r.pass_fail}**")
    lines.append(f"- **Reason:** {r.reason}")
    lines.append(f"- **Latency:** {r.latency_ms} ms")
    lines.append("")
    lines.append("## 3. ATV-2080 coverage")
    lines.append("")
    lines.append(
        f"- **Dimension:** {r.atv_dim}-D"
        f" · **Non-zero subfields:** {r.n_subfields_nonzero}/{r.n_subfields_total}"
        f" · **SHA3-256:** `{r.atv_sha3[:24]}…`"
    )
    lines.append("")
    lines.append("| # | Subfield | Slice | Non-zero | Max\\|val\\| |")
    lines.append("|---|---|---|:---:|---:|")
    for i, row in enumerate(r.subfield_coverage, 1):
        flag = "✓" if row["non_zero"] else " "
        lines.append(
            f"| {i} | `{row['subfield']}` | "
            f"{row['slice_start']}–{row['slice_stop']-1} | "
            f"{flag} | {row['max_abs']} |"
        )
    lines.append("")
    if r.m13_top_contributors:
        lines.append("## 4. M13 attribution (top 5)")
        lines.append("")
        for c in r.m13_top_contributors:
            lines.append(f"- `{c['subfield']}` — weight {c['weight']:.3f}")
        lines.append("")
    lines.append("## 5. Firewall step traces")
    lines.append("")
    lines.append(
        f"- **Total steps:** {r.n_steps}"
        f" · **Steps that emitted BLOCK / REQUIRE_APPROVAL:** {r.n_steps_blocking}"
    )
    lines.append("")
    lines.append("| Step | Trace |")
    lines.append("|---|---|")
    for step, trace in r.step_traces.items():
        # short module name for readability
        short = step.rsplit(".", 1)[-1] if "." in step else step
        # markdown-escape pipes in trace
        clean = (trace or "").replace("|", "\\|")
        lines.append(f"| `{short}` | {clean[:200]}{'…' if len(clean) > 200 else ''} |")
    lines.append("")
    if r.extras:
        lines.append("## 6. Extras")
        lines.append("")
        for k, v in r.extras.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")
    return "\n".join(lines)


def write_report(
    r: ScenarioReport,
    out_dir: Path,
    *,
    formats: tuple[str, ...] = ("md", "json"),
) -> dict[str, Path]:
    """Write the report to ``out_dir/scenario_{id}_{timestamp}.{ext}``.

    Returns ``{format: path}`` for each format written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = f"scenario_{r.scenario_id}_{ts}"
    written: dict[str, Path] = {}
    if "md" in formats:
        md_path = out_dir / f"{base}.md"
        md_path.write_text(to_markdown(r), encoding="utf-8")
        written["md"] = md_path
    if "json" in formats:
        json_path = out_dir / f"{base}.json"
        json_path.write_text(
            json.dumps(asdict(r), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        written["json"] = json_path
    return written


__all__ = ["ScenarioReport", "build_report", "to_markdown", "write_report"]
