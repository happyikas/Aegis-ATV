"""Render the AEGIS ATV-2080-v1 structure diagram to PNG.

Mirrors the visual language of the patent figure: title strip,
top-row 5-section ATV layout, three info boxes, horizontal pipeline.
Replaces the patent's per-instruction provenance section with the
actual shipped 30-subfield SW/HW band layout.

Run::

    uv run python docs/diagrams/draw_atv_2080_v1.py

Writes ``docs/diagrams/atv_2080_v1.png`` (2400 × 1900, ~340 KB).
The output is byte-stable across runs (no timestamps, no random
state, fixed font fallback path).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_OUT_PATH = Path(__file__).resolve().parent / "atv_2080_v1.png"

W, H = 2400, 1900
BG = (251, 251, 252)
TITLE_BLUE = (28, 49, 132)
SUBTITLE_GRAY = (87, 99, 122)
BORDER = (60, 70, 90)
LIGHT_BORDER = (180, 188, 200)

COL_HEADER  = (255, 247, 220)
COL_SW      = (220, 245, 230)
COL_HW      = (255, 234, 210)
COL_CRYPTO  = (228, 220, 248)
COL_FOOTER  = (210, 220, 240)

PIPE_COLORS = [
    (235, 244, 255),
    (220, 245, 230),
    (220, 245, 230),
    (220, 245, 230),
    (255, 234, 210),
    (255, 234, 210),
    (255, 234, 210),
    (255, 234, 210),
    (228, 220, 248),
    (228, 220, 248),
    (228, 220, 248),
    (210, 220, 240),
]


def _f(size, weight="regular"):
    try:
        if weight == "bold":
            return ImageFont.truetype(
                "/System/Library/Fonts/HelveticaNeue.ttc", size, index=2,
            )
        if weight == "mono":
            return ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size)
        return ImageFont.truetype(
            "/System/Library/Fonts/HelveticaNeue.ttc", size, index=0,
        )
    except Exception:
        return ImageFont.load_default()


def text_w(d, s, font):
    bbox = d.textbbox((0, 0), s, font=font)
    return bbox[2] - bbox[0]


def round_rect(d, xy, fill, outline=BORDER, radius=12, width=2):
    d.rounded_rectangle(
        xy, radius=radius, fill=fill, outline=outline, width=width,
    )


def cell(d, xy, fill, title, sub1, sub2="", title_font=None, sub_font=None):
    round_rect(d, xy, fill=fill, radius=10, width=1, outline=LIGHT_BORDER)
    cx = (xy[0] + xy[2]) // 2
    title_font = title_font or _f(16, "bold")
    sub_font = sub_font or _f(13)
    cy = xy[1] + 12
    d.text(
        (cx - text_w(d, title, title_font) // 2, cy),
        title, fill=(20, 30, 50), font=title_font,
    )
    cy += 24
    for line in (sub1, sub2):
        if line:
            d.text(
                (cx - text_w(d, line, sub_font) // 2, cy),
                line, fill=(50, 60, 80), font=sub_font,
            )
            cy += 19


def main():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Title
    title_font = _f(46, "bold")
    sub_font = _f(22)
    title = "AEGIS AGENT TELEMETRY VECTOR (ATV-2080-v1)"
    sub = (
        "Cryptographically Signed, Hardware-Anchored Audit Record"
        "  ·  shipped today  ·  30 subfields x 2,080 float32 = 8,320 bytes"
    )
    d.text(
        (W // 2 - text_w(d, title, title_font) // 2, 28),
        title, fill=TITLE_BLUE, font=title_font,
    )
    d.text(
        (W // 2 - text_w(d, sub, sub_font) // 2, 86),
        sub, fill=SUBTITLE_GRAY, font=sub_font,
    )

    # Section labels (top row 5-band)
    sect_y = 145
    sect_h = 40
    label_font = _f(15, "bold")
    sects = [
        ("ATV HEADER",                              80,   400),
        ("SW BAND  (19 subfields x 1880-D)",        410,  1100),
        ("HW BAND  (11 subfields x 200-D x T3)",    1110, 1700),
        ("ATTESTATION CHAIN",                       1710, 2080),
        ("CRYPTOGRAPHIC FOOTER",                    2090, 2320),
    ]
    band_colors = [COL_HEADER, COL_SW, COL_HW, COL_CRYPTO, COL_FOOTER]
    for (lbl, x0, x1), col in zip(sects, band_colors, strict=True):
        round_rect(
            d, (x0, sect_y, x1, sect_y + sect_h),
            fill=col, radius=8, width=1, outline=LIGHT_BORDER,
        )
        d.text((x0 + 14, sect_y + 11), lbl, fill=(40, 50, 80), font=label_font)

    # Top-row subfield cells
    row_y = sect_y + sect_h + 14
    row_h = 110

    header_cells = [
        ("trace_id",     "16-byte UUID"),
        ("span_id",      "16-byte UUID"),
        ("tenant_id",    "string"),
        ("aid",          "M14 UUID"),
        ("timestamp_ns", "int64"),
        ("model_hash",   "SHA3-256"),
        ("atv_hash",     "SHA3-256"),
    ]
    hdr_x0, hdr_x1 = 80, 400
    cw = (hdr_x1 - hdr_x0 - 16) / len(header_cells)
    for i, (lbl, det) in enumerate(header_cells):
        x = int(hdr_x0 + 8 + i * cw)
        cell(
            d, (x, row_y, int(x + cw - 4), row_y + row_h),
            fill=COL_HEADER, title=lbl, sub1=det,
            title_font=_f(11, "bold"), sub_font=_f(10),
        )

    # SW band — 19 subfields, 4 cols x 5 rows
    sw_x0, sw_x1 = 410, 1100
    sw_cells = [
        ("agent_state_embedding",     768, "0..767"),
        ("action_history",            640, "768..1407"),
        ("inter_agent_graph",         128, "1408..1535"),
        ("memory_provenance",          64, "1536..1599"),
        ("qom_scores",                 16, "1600..1615"),
        ("resource_access_pattern",    32, "1616..1647"),
        ("prompt_structure",           16, "1648..1663"),
        ("aid_ats_scalars",             8, "1664..1671"),
        ("encryption_metadata",        12, "1672..1683"),
        ("output_content_fingerprint", 64, "1684..1747"),
        ("tool_arg_inspection",        32, "1748..1779"),
        ("action_blast_radius",        16, "1780..1795"),
        ("output_channel_diversity",   12, "1796..1807"),
        ("session_behavioral_drift",   16, "1808..1823"),
        ("mcp_trust_signals",          12, "1824..1835"),
        ("grounding_metrics",          16, "1836..1851"),
        ("novelty_score",               4, "1852..1855"),
        ("human_oversight_state",       8, "1856..1863"),
        ("cost_efficiency_metrics",    16, "1864..1879"),
    ]
    cols = 4
    rows_n = 5
    cw_sw = (sw_x1 - sw_x0 - 8) / cols
    rh_sw = (row_h + 280) / rows_n
    sw_total_h = int(rh_sw * rows_n) + 10
    for i, (lbl, dim, rng) in enumerate(sw_cells):
        r, c = divmod(i, cols)
        x = int(sw_x0 + 4 + c * cw_sw)
        y = row_y + int(r * rh_sw)
        cell(
            d, (x, y, int(x + cw_sw - 4), int(y + rh_sw - 6)),
            fill=COL_SW, title=lbl, sub1=f"{dim}-D", sub2=rng,
            title_font=_f(11, "bold"), sub_font=_f(10, "mono"),
        )

    # HW band — 11 subfields, 3 cols x 4 rows
    hw_x0, hw_x1 = 1110, 1700
    hw_cells = [
        ("memory_timing_histograms",     32, "1880..1911"),
        ("aid_tag_transitions",          24, "1912..1935"),
        ("atmu_anomaly",                 16, "1936..1951"),
        ("dma_fanout",                   16, "1952..1967"),
        ("thermal_ecc_drift",            16, "1968..1983"),
        ("watchdog_signals",             12, "1984..1995"),
        ("network_telemetry",            24, "1996..2019"),
        ("gpu_accelerator_state",        16, "2020..2035"),
        ("hypervisor_signals",            8, "2036..2043"),
        ("hw_cost_attestation",          16, "2044..2059"),
        ("linkage_consistency_features", 20, "2060..2079"),
    ]
    cols_hw = 3
    cw_hw = (hw_x1 - hw_x0 - 8) / cols_hw
    rh_hw = sw_total_h / 4
    for i, (lbl, dim, rng) in enumerate(hw_cells):
        r, c = divmod(i, cols_hw)
        x = int(hw_x0 + 4 + c * cw_hw)
        y = row_y + int(r * rh_hw)
        cell(
            d, (x, y, int(x + cw_hw - 4), int(y + rh_hw - 6)),
            fill=COL_HW, title=lbl, sub1=f"{dim}-D", sub2=rng,
            title_font=_f(10, "bold"), sub_font=_f(10, "mono"),
        )
    last_x = int(hw_x0 + 4 + 2 * cw_hw)
    last_y = row_y + int(3 * rh_hw)
    cell(
        d,
        (last_x, last_y, int(last_x + cw_hw - 4), int(last_y + rh_hw - 6)),
        fill=(245, 235, 215), title="(reserved)", sub1="T3 expansion",
        title_font=_f(10), sub_font=_f(9),
    )

    # Attestation chain (4 cells in 2x2)
    att_x0, att_x1 = 1710, 2080
    att_cells = [
        ("prev_hash",    "SHA3-256",  "Merkle chain"),
        ("this_hash",    "SHA3-256",  "32 byte"),
        ("Ed25519 sig",  "64 byte",   "M5 + M14"),
        ("AES-GCM",      "journal",   "M15"),
    ]
    cw_att = (att_x1 - att_x0 - 8) / 2
    rh_att = (sw_total_h - 10) / 2
    for i, (a, b, c2) in enumerate(att_cells):
        r, c = divmod(i, 2)
        x = int(att_x0 + 4 + c * cw_att)
        y = row_y + int(r * rh_att)
        cell(
            d, (x, y, int(x + cw_att - 4), int(y + rh_att - 6)),
            fill=COL_CRYPTO, title=a, sub1=b, sub2=c2,
            title_font=_f(12, "bold"), sub_font=_f(10),
        )

    # Footer cell
    foot_x0, foot_x1 = 2090, 2320
    cell(
        d, (foot_x0, row_y, foot_x1, row_y + sw_total_h - 16),
        fill=COL_FOOTER, title="ATV signature", sub1="Ed25519 64 byte",
        title_font=_f(13, "bold"), sub_font=_f(11, "mono"),
    )
    d.text((foot_x0 + 16, row_y + 88),  "·  step360 audit",      fill=(50, 60, 80), font=_f(11))
    d.text((foot_x0 + 16, row_y + 110), "·  invalidates on",     fill=(50, 60, 80), font=_f(11))
    d.text((foot_x0 + 16, row_y + 130), "   any tampering",      fill=(50, 60, 80), font=_f(11))
    d.text((foot_x0 + 16, row_y + 158), "·  verify-audit",       fill=(50, 60, 80), font=_f(11))
    d.text((foot_x0 + 16, row_y + 180), "   walks chain",        fill=(50, 60, 80), font=_f(11))
    d.text((foot_x0 + 16, row_y + 208), "·  v3.1 RAG-aware",     fill=(50, 60, 80), font=_f(11))
    d.text((foot_x0 + 16, row_y + 230), "   for replay",         fill=(50, 60, 80), font=_f(11))

    # Total length banner
    banner_y = row_y + sw_total_h + 12
    banner_text = (
        "TOTAL ATV LENGTH:  2,080 float32 = 8,320 bytes"
        "    +  ATVHeader (~250 bytes JSON)    +  Ed25519 signature (64 bytes)"
    )
    d.text(
        (W // 2 - text_w(d, banner_text, _f(15, "bold")) // 2, banner_y),
        banner_text, fill=(30, 40, 60), font=_f(15, "bold"),
    )

    # ── Three info boxes ────────────────────────────────────────────
    box_y = banner_y + 40
    box_h = 720

    # Box 1: Step → subfield map
    b1_x0, b1_x1 = 60, 800
    round_rect(d, (b1_x0, box_y, b1_x1, box_y + box_h), fill=(255, 255, 255), radius=12, width=2)
    d.text(
        (b1_x0 + 16, box_y + 14),
        "FIREWALL STEP → ATV SUBFIELD CONTRIBUTION",
        fill=TITLE_BLUE, font=_f(17, "bold"),
    )
    map_rows = [
        ("step305 safe_allowlist",    "→ safe_fast_path flag"),
        ("step308 identity",          "→ aid_ats_scalars"),
        ("step309 instruction_drift", "→ mcp_trust_signals"),
        ("step310 args (regex)",      "→ tool_arg_inspection"),
        ("step311 donor_rules",       "→ tool_arg_inspection"),
        ("step312 normalize",         "→ output_content_fingerprint"),
        ("step315 aid_auth (M14)",    "→ aid_ats_scalars"),
        ("step320 blast_radius",      "→ action_blast_radius"),
        ("step330 human",             "→ human_oversight_state"),
        ("step335 cost (M12)",        "→ cost_efficiency_metrics"),
        ("step336 loop_detector",     "→ session_behavioral_drift"),
        ("step337 hw_anomaly (T3)",   "→ atmu_anomaly + thermal"),
        ("step340 policy + sLLM",     "→ FULL ATV via M13 head"),
        ("step350 approval",          "→ verdict + queue insert"),
        ("step360 audit",             "→ chain advance + signature"),
        ("step370 emit",              "→ ALLOW / BLOCK / APPROVAL"),
    ]
    yy = box_y + 50
    for i, (a, b) in enumerate(map_rows):
        bg = (245, 248, 252) if i % 2 == 0 else (255, 255, 255)
        d.rectangle((b1_x0 + 8, yy - 4, b1_x1 - 8, yy + 24), fill=bg)
        d.text((b1_x0 + 24, yy), a, fill=(20, 30, 60), font=_f(13, "mono"))
        d.text((b1_x0 + 360, yy), b, fill=(50, 80, 50), font=_f(13, "mono"))
        yy += 28
    d.text(
        (b1_x0 + 24, yy + 14),
        "16 firewall steps + post-tool feedback",
        fill=SUBTITLE_GRAY, font=_f(12),
    )

    # Box 2: cost-efficiency 16 named slots
    b2_x0, b2_x1 = 820, 1530
    round_rect(d, (b2_x0, box_y, b2_x1, box_y + box_h), fill=(255, 255, 255), radius=12, width=2)
    d.text(
        (b2_x0 + 16, box_y + 14),
        "COST_EFFICIENCY_METRICS — 16 NAMED SLOTS",
        fill=TITLE_BLUE, font=_f(17, "bold"),
    )
    d.text(
        (b2_x0 + 16, box_y + 42),
        "the only fully-named subfield  ·  step335 + cost-optimizer advisor",
        fill=SUBTITLE_GRAY, font=_f(11),
    )
    cost_slots = [
        ("s-1",  "input_token_count",                     "current step"),
        ("s-2",  "output_token_count",                    "current step"),
        ("s-3",  "reasoning_token_count",                 "current step"),
        ("s-4",  "cumulative_tokens",                     "trace"),
        ("s-5",  "cumulative_dollars",                    "trace"),
        ("s-6",  "tokens_per_successful_tool_invocation", "ratio"),
        ("s-7",  "tokens_per_plan_step_completed",        "ratio"),
        ("s-8",  "tokens_per_byte_of_final_output",       "ratio"),
        ("s-9",  "reasoning_to_action_ratio",             "ratio"),
        ("s-10", "cache_hit_rate",                        "ratio"),
        ("s-11", "context_utilization_ratio",             "ratio"),
        ("s-12", "cost_delta_vs_role_baseline",           "baseline"),
        ("s-13", "budget_burn_rate",                      "forecast"),
        ("s-14", "forecasted_cost_to_completion",         "forecast"),
        ("s-15", "task_progress_score",                   "forecast"),
        ("s-16", "marginal_value_score",                  "forecast"),
    ]
    yy = box_y + 78
    for i, (slot, name, kind) in enumerate(cost_slots):
        bg = (250, 246, 240) if i % 2 == 0 else (255, 252, 248)
        d.rectangle((b2_x0 + 8, yy - 4, b2_x1 - 8, yy + 32), fill=bg)
        d.text((b2_x0 + 24, yy + 4), slot, fill=(150, 90, 30), font=_f(13, "mono"))
        d.text((b2_x0 + 80, yy + 4), name, fill=(20, 30, 60), font=_f(13, "mono"))
        d.text((b2_x0 + 480, yy + 4), kind, fill=(80, 100, 130), font=_f(11))
        yy += 36

    # Box 3: JSON example
    b3_x0, b3_x1 = 1550, 2340
    round_rect(d, (b3_x0, box_y, b3_x1, box_y + box_h), fill=(255, 255, 255), radius=12, width=2)
    d.text(
        (b3_x0 + 16, box_y + 14),
        "EXAMPLE — ABBREVIATED JSON",
        fill=TITLE_BLUE, font=_f(17, "bold"),
    )
    json_lines = [
        "{",
        '  "header": {',
        '    "schema_version":     "ATV-2080-v1",',
        '    "trace_id":           "a3f6c2e8-7b19...",',
        '    "span_id":            "8d9b0f47-2c11...",',
        '    "parent_span_id":     null,',
        '    "tenant_id":          "claude-code-local",',
        '    "aid":                "f1c2-...",',
        '    "timestamp_ns":       1714857655000000000,',
        '    "tier_profile":       "T2",',
        '    "cost_attestation":   "software",',
        '    "model_hash":         "sha3:9f3a...c771",',
        '    "burn_in_id":         "burnin-2026-w18",',
        '    "atv_hash":           "sha3:7b91...44aa"',
        "  },",
        '  "tensor": float32[2080],',
        '  "subfield_attribution": {        // M13 head',
        '    "tool_arg_inspection":      0.82,',
        '    "action_blast_radius":      0.71,',
        '    "session_behavioral_drift": 0.65,',
        '    "cost_efficiency_metrics":  0.40',
        "  },",
        '  "verdict": {',
        '    "decision":   "BLOCK",',
        '    "confidence": 0.92,',
        '    "reason":     "rule:fs_destructive"',
        "  },",
        '  "rag_hits": [        // PR #88+ retrieval',
        '    "rule-fs-destructive",',
        '    "playbook-loop-cost-runaway"',
        "  ],",
        '  "anchor_ts_ns": 1714857655000000000,  // PR #95',
        '  "prev_hash":  "sha3:ed23...8a9e",',
        '  "this_hash":  "sha3:a0a0...05bc",',
        '  "signature":  "ed25519:BASE64..."',
        "}",
    ]
    yy = box_y + 50
    for line in json_lines:
        d.text(
            (b3_x0 + 20, yy), line,
            fill=(40, 50, 90) if line.startswith("  ") else (20, 30, 60),
            font=_f(11, "mono"),
        )
        yy += 17

    # ── Pipeline at the bottom ──────────────────────────────────────
    pipe_y = box_y + box_h + 50
    d.text(
        (W // 2 - 280, pipe_y),
        "ATV PIPELINE  ·  PreToolUse hook → 16 firewall steps → PostToolUse hook",
        fill=TITLE_BLUE, font=_f(20, "bold"),
    )

    pipe_y_box = pipe_y + 36
    pipe_h = 130
    pipe_stages = [
        ("1. Ingestion",    "PreToolUse\nhook receives\n{tool, args}"),
        ("2. build_atv",    "30-subfield\ntensor +\nATVHeader"),
        ("3. step305..312", "allowlist,\nidentity, drift,\nargs, normalize"),
        ("4. step315",      "aid_auth (M14)\ncircuit breaker"),
        ("5. step320..336", "blast, human,\ncost (M12),\nloop"),
        ("6. step337",      "hw_anomaly (T3)\nATMU/thermal"),
        ("7. step340",      "policy + sLLM\n+ M13 head\n+ RAG block"),
        ("8. step350",      "approval queue"),
        ("9. step360",      "audit:\nsign + Merkle\n+ AES-GCM"),
        ("10. step370",     "ALLOW / BLOCK /\nAPPROVAL\n→ stdout"),
        ("11. PostToolUse", "retro outcome"),
        ("12. ATMU 2PC",    "commit /\ncompensate"),
    ]
    n = len(pipe_stages)
    pad = 8
    available = W - 2 * 60
    pcw = (available - (n - 1) * pad) / n

    for i, (head, body) in enumerate(pipe_stages):
        x = int(60 + i * (pcw + pad))
        col = PIPE_COLORS[i] if i < len(PIPE_COLORS) else COL_FOOTER
        round_rect(
            d, (x, pipe_y_box, int(x + pcw), pipe_y_box + pipe_h),
            fill=col, radius=10, width=1, outline=LIGHT_BORDER,
        )
        d.text((x + 12, pipe_y_box + 10), head, fill=TITLE_BLUE, font=_f(12, "bold"))
        for j, line in enumerate(body.split("\n")):
            d.text(
                (x + 12, pipe_y_box + 36 + j * 18), line,
                fill=(40, 50, 80), font=_f(11),
            )
        if i < n - 1:
            ax = int(x + pcw)
            ay = pipe_y_box + pipe_h // 2
            d.polygon(
                [(ax + 1, ay - 5), (ax + pad - 1, ay), (ax + 1, ay + 5)],
                fill=BORDER,
            )

    # Footer notes
    foot_y = pipe_y_box + pipe_h + 24
    notes = [
        "·  ATV is written into the Write-Ahead Intent Log (W-AIL) before tool invocation; signed inside the Ed25519 footer.",
        "·  Any tampering invalidates the chain — `aegis verify-audit` walks it from genesis.",
        "·  v3.1 temporal-RAG (PR #94..#98) adds chunk-level valid_from/valid_until/supersedes/created_at + retrieve(anchor_ts_ns).",
        "·  M13 attribution head emits 30-D contribution scores against the 30 subfields above — what step340 uses to pick a verdict.",
    ]
    for i, line in enumerate(notes):
        d.text((60, foot_y + i * 22), line, fill=SUBTITLE_GRAY, font=_f(13))

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    img.save(_OUT_PATH, optimize=True)
    print("wrote", _OUT_PATH, f"({W}x{H})")


if __name__ == "__main__":
    main()
