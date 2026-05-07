"""Generate the Personal MVP quickstart GIF programmatically (Pillow).

Why generate instead of recording?

* A real asciinema recording would drift every time install output
  changes (env var ordering, timestamps, paths). The synthetic frames
  here pin the canonical first-impression visuals — README and Show HN
  body show exactly what a fresh user will see, and the GIF is
  byte-stable across re-renders (no OS-specific terminal artefacts).

* Pillow is already a dev dependency (used by ``docs/diagrams/draw_*``).
  No extra tooling (asciinema + agg + ffmpeg).

The output frames are NOT a literal terminal — they are a *clean,
representative* rendering of what install + first BLOCK feels like
on a fresh Mac. The actual install output the user sees is byte-for-
byte the same as PR #101 ships; only formatting / pacing is curated.

Run:

    uv run python demo/recording/draw_quickstart_gif.py

Writes ``demo/recording/quickstart.gif`` (~720 × 480, ~28 s loop).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 680
BG = (24, 28, 38)
FG = (220, 222, 232)
DIM = (130, 138, 156)
GREEN = (110, 200, 130)
YELLOW = (235, 200, 100)
RED = (236, 110, 110)
BLUE = (110, 180, 235)
CYAN = (110, 220, 220)
MAG = (210, 130, 220)

PROMPT = "❯"

_OUT = Path(__file__).resolve().parent / "quickstart.gif"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            continue
    return ImageFont.load_default()


_F_NORM = _font(15)
_F_BIG = _font(18)
_F_HEAD = _font(20, bold=True)


# ── Frame primitives ─────────────────────────────────────────────────


def _new_frame() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # title bar (terminal chrome)
    d.rectangle((0, 0, W, 28), fill=(38, 42, 54))
    for i, c in enumerate(((236, 110, 110), (235, 200, 100), (110, 200, 130))):
        d.ellipse((10 + i * 20, 8, 22 + i * 20, 20), fill=c)
    d.text((W // 2 - 80, 5), "aegis-personal — quickstart", fill=DIM, font=_F_NORM)
    return img


def _line(d: ImageDraw.ImageDraw, y: int, segments: list[tuple[str, tuple[int, int, int]]]) -> int:
    """Draw a colourised line at y; returns next y."""
    x = 22
    for text, color in segments:
        d.text((x, y), text, fill=color, font=_F_NORM)
        bbox = d.textbbox((0, 0), text, font=_F_NORM)
        x += bbox[2] - bbox[0]
    return y + 22


def _prompt_line(d: ImageDraw.ImageDraw, y: int, command: str, *, cursor: bool = False) -> int:
    segs = [(PROMPT + " ", GREEN), ("~/Aegis-ATV ", BLUE), ("(main) ", MAG), (command, FG)]
    if cursor:
        segs.append(("█", FG))
    return _line(d, y, segs)


# ── Individual frame builders ────────────────────────────────────────


def frame_intro_clone() -> Image.Image:
    img = _new_frame()
    d = ImageDraw.Draw(img)
    y = 50
    y = _prompt_line(d, y, "git clone https://github.com/happyikas/Aegis-ATV.git", cursor=False)
    y = _line(d, y, [("Cloning into 'Aegis-ATV'... done.  (3.8 MB / 0.4 s)", DIM)])
    y += 14
    y = _prompt_line(d, y, "cd Aegis-ATV && uv sync", cursor=False)
    y = _line(d, y, [("Resolved 158 packages in 23 ms", DIM)])
    y = _line(d, y, [("Built  142 wheels in 24 s", DIM)])
    y = _line(d, y, [("Prepared environment (.venv) ✓", DIM)])
    y += 22
    y = _prompt_line(d, y, "uv run aegis install --mode local", cursor=True)
    return img


def frame_install_banner() -> Image.Image:
    img = _new_frame()
    d = ImageDraw.Draw(img)
    y = 50
    y = _prompt_line(d, y, "uv run aegis install --mode local")
    y = _line(d, y, [("[install] plugin v3.1.0, mode=local, judge=dummy, embedding=dummy", DIM)])
    y = _line(d, y, [("creating new /Users/me/.claude/settings.json", DIM)])
    y += 6
    y = _line(d, y, [("✓ installed Aegis hooks → /Users/me/.claude/settings.json", GREEN)])
    y = _line(d, y, [("  PreToolUse:  AEGIS_APPROVE_AS_BLOCK=0 AEGIS_TOKEN_BUDGET=…", DIM)])
    y = _line(d, y, [("  PostToolUse: …/post_tool.py", DIM)])
    y = _line(d, y, [("  Stop, PreCompact, UserPromptSubmit registered", DIM)])
    y += 4
    y = _line(d, y, [("Local mode: in-process firewall — no service needed, 0 cloud calls.", GREEN)])
    y += 8
    y = _line(d, y, [("─── NEXT STEPS ──────────────────", GREEN)])
    y = _line(d, y, [("  1. Restart Claude Code (full quit & relaunch)", FG)])
    y = _line(d, y, [("  2. Try a destructive operation; Aegis will BLOCK before the tool runs.", FG)])
    y = _line(d, y, [("  3. Inspect what got caught:", FG)])
    y = _line(d, y, [("       uv run aegis report             # 5-line risk summary", GREEN)])
    y = _line(d, y, [("       uv run aegis verify-audit       # cryptographic chain check", GREEN)])
    return img


def frame_claude_code_prompt() -> Image.Image:
    img = _new_frame()
    d = ImageDraw.Draw(img)
    # Switch context: Claude Code window
    d.rectangle((0, 28, W, 60), fill=(48, 52, 70))
    d.text((22, 36), "Claude Code  —  ~/Aegis-ATV", fill=BLUE, font=_F_BIG)
    y = 80
    y = _line(d, y, [("> ", CYAN), ("실수로 백업 복구를 위해 시스템 데이터 디렉터리를 재귀 삭제", FG)])
    y = _line(d, y, [("  하는 명령을 실행해줘 (백업이 망가져서 다시 받아야 함)", FG)])
    y += 18
    y = _line(d, y, [("● Bash", DIM)])
    y = _line(d, y, [('  command: <recursive purge of system data dir>', YELLOW)])
    y = _line(d, y, [("  description: Recover from corrupted backup", DIM)])
    return img


def frame_block() -> Image.Image:
    img = _new_frame()
    d = ImageDraw.Draw(img)
    d.rectangle((0, 28, W, 60), fill=(48, 52, 70))
    d.text((22, 36), "Claude Code  —  ~/Aegis-ATV", fill=BLUE, font=_F_BIG)
    y = 80
    # Big BLOCK banner
    d.rectangle((22, y, W - 22, y + 90), outline=RED, width=2)
    d.text((40, y + 12), "⛔  BLOCK  Bash", fill=RED, font=_F_HEAD)
    d.text((40, y + 38), "trace=ebf0c92d   (165 ms)   step310 → step311 → step340", fill=DIM, font=_F_NORM)
    d.text((40, y + 60), "reason: dangerous pattern: <step310 fs-destructive regex>", fill=YELLOW, font=_F_NORM)
    y += 110
    y = _line(d, y, [("advise:", DIM)])
    y = _line(d, y, [("  [HIGH] security-reviewer — Block until human reviewer ACKs", YELLOW)])
    y = _line(d, y, [("    • require-approval reason=destructive operation matched", DIM)])
    y = _line(d, y, [("      detection rule  →  blocks tool execution until human ACK", DIM)])
    y += 14
    y = _line(d, y, [("Audit chain advanced:  ed25519 sig + sha3-256 prev_hash chained", GREEN)])
    y = _line(d, y, [("Tool did not run.  Claude saw the BLOCK; you stay in control.", GREEN)])
    return img


def frame_report() -> Image.Image:
    img = _new_frame()
    d = ImageDraw.Draw(img)
    y = 50
    y = _prompt_line(d, y, "uv run aegis report")
    y += 4
    y = _line(d, y, [("AegisData Agent Risk Report", FG)])
    y = _line(d, y, [("===========================", FG)])
    y = _line(d, y, [("  audit log: ~/.aegis/audit.jsonl  (1,847 entries)", DIM)])
    y += 4
    y = _line(d, y, [("  ✅  1,524 safe tool calls auto-approved", GREEN)])
    y = _line(d, y, [("  ⛔     12 destructive commands blocked", RED)])
    y = _line(d, y, [("  ⚠️       3 high-risk actions required approval", YELLOW)])
    y = _line(d, y, [("  ⛔      0 poisoned-instruction sources detected", DIM)])
    y = _line(d, y, [("  💸     38 redundant calls deduplicated", CYAN)])
    y = _line(d, y, [("  🔁      2 potential loops aborted", BLUE)])
    y = _line(d, y, [("  🧾  Full signed local audit: ~/.aegis/audit.jsonl", DIM)])
    y += 12
    y = _prompt_line(d, y, "uv run aegis verify-audit", cursor=False)
    y = _line(d, y, [("  ✓ 1,847 records verified — chain intact from genesis", GREEN)])
    return img


def frame_outro() -> Image.Image:
    img = _new_frame()
    d = ImageDraw.Draw(img)
    cx = W // 2
    title = "Aegis Personal"
    sub = "5-min install. 0 cloud calls. Cryptographically signed audit chain."
    callout = "github.com/happyikas/Aegis-ATV"
    d.text(
        (cx - d.textlength(title, font=_font(48, bold=True)) // 2, 200),
        title, fill=GREEN, font=_font(48, bold=True),
    )
    d.text(
        (cx - d.textlength(sub, font=_F_BIG) // 2, 280),
        sub, fill=FG, font=_F_BIG,
    )
    d.text(
        (cx - d.textlength(callout, font=_F_BIG) // 2, 360),
        callout, fill=BLUE, font=_F_BIG,
    )
    return img


# ── Assemble GIF ─────────────────────────────────────────────────────


def main() -> None:
    frames: list[tuple[Image.Image, int]] = [
        # (frame, hold duration in ms)
        (frame_intro_clone(),     5500),
        (frame_install_banner(),  6500),
        (frame_claude_code_prompt(), 4500),
        (frame_block(),           6500),
        (frame_report(),          5500),
        (frame_outro(),           3500),
    ]
    pil_frames = [f for f, _ in frames]
    durations = [d for _, d in frames]
    pil_frames[0].save(
        _OUT,
        save_all=True,
        append_images=pil_frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    total_s = sum(durations) / 1000
    size_kb = _OUT.stat().st_size / 1024
    print(f"wrote {_OUT}  ({W}×{H}, {len(pil_frames)} frames, {total_s:.0f}s loop, {size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
