"""``aegis tour`` — interactive 60-second onboarding.

Why a tour
----------

Docs are great for reference but bad for onboarding — most users
don't read them. ``aegis tour`` is a rich-based panel walkthrough
that takes a user from "what is this?" to "where do I start?" in
about a minute, with no prior knowledge.

Each step is one Panel. User presses Enter to advance, ``q`` to
quit. The tour itself never modifies any state — it's pure read
+ display, safe to re-run anytime.

Design
------

* **Plain-language first** — no patent vocabulary on slide 1
* **One concept per panel** — no info overload
* **Color + emoji** as visual cues (matches USER_GUIDE.ko.md tone)
* **Always show next action** — don't leave the user wondering what's
  next
* **Quit anytime** — q exits cleanly, no surprise side effects
* **Demo-friendly** — runs identically with or without ContextMemory

Steps
-----

1. Welcome — "what is Aegis?"
2. 자물쇠 · CCTV · 영수증 비유
3. Below-the-model chokepoint 도해
4. 가상의 "rm -rf production" 시연
5. 5분 설치 + 첫 명령어
6. 3가지 핵심 기능 (Coach / Live / Doctor)
7. 다음 단계 (dashboard 추천 + docs link)

7 steps × ~9 seconds = ~60 seconds total.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from rich.align import Align
from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

TOTAL_STEPS = 7


# ── step data ────────────────────────────────────────────────────


@dataclass(frozen=True)
class TourStep:
    """One panel in the tour. Pure data — :func:`render_step` does
    the rich-formatted output."""

    number: int           # 1-indexed
    title: str
    eyebrow: str          # small label above title (e.g. "STEP 1 OF 7")
    body: tuple[str, ...]  # paragraphs
    next_hint: str        # what to do after this panel


TOUR_STEPS: tuple[TourStep, ...] = (
    TourStep(
        number=1,
        title="🛡️  Aegis 에 오신 것을 환영합니다",
        eyebrow="STEP 1 OF 7  ·  WELCOME",
        body=(
            "Aegis 는 AI 에이전트가 실수 또는 공격으로 시스템을 망가뜨리지 못하게 막고,",
            "동시에 모든 행동을 위조 불가능한 기록으로 남기는 도구입니다.",
            "",
            "이 짧은 안내는 60초 정도 걸립니다.  Enter 키로 진행, q 로 종료.",
        ),
        next_hint="Enter ▶  계속",
    ),
    TourStep(
        number=2,
        title="세 가지 비유로 이해하기",
        eyebrow="STEP 2 OF 7  ·  CONCEPT",
        body=(
            "🚪  자물쇠         AI 가 위험한 명령을 실행하기 직전에 막습니다",
            "📹  CCTV          AI 의 모든 행동을 시간 순서대로 기록합니다",
            "🧾  공증 영수증    그 기록은 암호로 서명되어 누구도 위조 못 합니다",
            "",
            "이 셋이 합쳐서 Aegis 의 핵심 가치를 만듭니다.",
        ),
        next_hint="Enter ▶  Aegis 가 어디에서 작동하나요?",
    ),
    TourStep(
        number=3,
        title="\"below the model\" — 결정과 실행 사이의 chokepoint",
        eyebrow="STEP 3 OF 7  ·  WHERE IT FITS",
        body=(
            "    ┌──────────────┐",
            "    │  AI 에이전트  │   Claude Code / OpenClaw / Codex / …",
            "    └──────┬───────┘",
            "           │ \"이 도구 호출하겠다\"",
            "           ▼",
            "    ┌──────────────┐",
            "    │  ★ Aegis     │   16-step firewall + 감사 체인",
            "    │              │   \"이 행동 안전한가?\" → 통과/승인/차단",
            "    └──────┬───────┘",
            "           │ 안전한 경우만",
            "           ▼",
            "    ┌──────────────┐",
            "    │  실제 도구    │   셸 · DB · API · 결제 · …",
            "    └──────────────┘",
            "",
            "모델의 안전 응답 필터보다 한 단계 더 아래에서 작동합니다.",
        ),
        next_hint="Enter ▶  실제 어떻게 작동하는지 보기",
    ),
    TourStep(
        number=4,
        title="시나리오 — \"삭제 사고\" 가 어떻게 막히나",
        eyebrow="STEP 4 OF 7  ·  DEMO",
        body=(
            "🔻 Aegis 없을 때",
            "    사용자: \"tmp 폴더 정리해줘\"",
            "    Claude:  → 시스템 폴더 대상 재귀 삭제 시도",
            "             → 실행됨, 시스템 망가짐  ❌",
            "",
            "✅ Aegis 있을 때",
            "    사용자: \"tmp 폴더 정리해줘\"",
            "    Claude:  → 같은 위험 명령 시도",
            "    Aegis:   ⛔ BLOCK trace=abc123 (45ms)",
            "             reason: 시스템 경로 대상 재귀 삭제",
            "             advise: security-reviewer (HIGH)",
            "",
            "→ 45ms 안에 차단, audit log 에 영구 기록",
        ),
        next_hint="Enter ▶  설치하기",
    ),
    TourStep(
        number=5,
        title="5분 설치 — 한 줄이면 됩니다",
        eyebrow="STEP 5 OF 7  ·  INSTALL",
        body=(
            "  $ uv tool install aegis-mvp",
            "  $ aegis install --mode local",
            "  (Claude Code 재시작)",
            "",
            "그 후 매일 쓰는 첫 5가지 명령:",
            "  aegis dashboard       한 화면 TUI — Cost · Perf · Security",
            "  aegis report          최근 24시간 5줄 요약",
            "  aegis doctor          종합 markdown 리포트",
            "  aegis verify-audit    감사 체인 무결성 (1초)",
            "  aegis forensic last   가장 최근 BLOCK 분석",
        ),
        next_hint="Enter ▶  3가지 핵심 기능",
    ),
    TourStep(
        number=6,
        title="🏋️ Coach  ·  📊 Live  ·  🔧 Doctor",
        eyebrow="STEP 6 OF 7  ·  FEATURES",
        body=(
            "🏋️  Coach   사용자 환경의 정상 패턴을 학습 (5-layer × 4-phase)",
            "             → sLLM judge 에 주입되어 \"평소와 다른지\" 판단",
            "",
            "📊  Live    Cost / Performance / Security 실시간 추적",
            "             → aegis dashboard 한 화면에 통합",
            "",
            "🔧  Doctor  사건 사후 분석 + 권고 + 시간 되돌리기",
            "             → 8명의 가상 advisor 가 자동 권고",
            "",
            "세 기능은 PitchDeck 의 5 기반 기술 (ATV · ATMU · sLLM · Crypto-Sign · Burn-in)",
            "을 사용자 친화 형태로 묶은 것입니다.",
        ),
        next_hint="Enter ▶  마지막 — 다음 단계",
    ),
    TourStep(
        number=7,
        title="🎉  안내 끝! 다음에 할 것",
        eyebrow="STEP 7 OF 7  ·  NEXT",
        body=(
            "지금 바로 해보세요:",
            "",
            "  $ aegis dashboard --demo",
            "    → 가상 데이터로 dashboard 미리 보기",
            "",
            "  $ aegis install --mode local",
            "    → Claude Code 에 후크 설치",
            "",
            "  $ aegis report",
            "    → 본인 환경의 첫 5줄 요약",
            "",
            "더 깊은 안내:  docs/USER_GUIDE.ko.md  (비전문가용 통합 가이드)",
            "통합 문서:    docs/integrations/  (Claude / OpenClaw / OpenRouter / Hermes / Serena)",
            "GitHub:       github.com/happyikas/Aegis-ATV",
        ),
        next_hint="Enter ▶  종료",
    ),
)


# ── rendering ───────────────────────────────────────────────────


def render_step(step: TourStep) -> Panel:
    """Render one tour step as a rich Panel.

    Layout:
      [eyebrow line, dim coral]
      [title, bold cyan, large]
      [divider line]
      [body paragraphs, dark]
      [divider line]
      [next hint, italic green]
    """
    eyebrow = Text(step.eyebrow, style="bold #F96167")  # coral
    title = Text(step.title, style="bold cyan")
    body_lines = "\n".join(step.body)
    body = Text(body_lines, style="white")
    next_hint = Text(step.next_hint, style="italic green")

    content: Any = Group(
        eyebrow,
        Padding(title, (1, 0, 1, 0)),
        body,
        Padding(next_hint, (1, 0, 0, 0)),
    )
    return Panel(
        Padding(content, (1, 2)),
        border_style="cyan",
        padding=(0, 0),
        title=f" [bold]{step.number}/{TOTAL_STEPS}[/bold] ",
        title_align="right",
    )


# ── runner ───────────────────────────────────────────────────────


def run_tour(
    *,
    console: Console | None = None,
    auto_advance: bool = False,
    input_func: Any = input,
) -> int:
    """Run the interactive tour.

    Parameters
    ----------
    console:
        Optional rich Console. Defaults to a fresh stdout Console.
    auto_advance:
        When True, skips the input() wait — useful for tests so the
        tour runs to completion without blocking.
    input_func:
        Injected for testability. Production calls :func:`input`.

    Returns
    -------
    int
        ``0`` on completion (or graceful quit). Never raises on
        normal flow.
    """
    console = console or Console()

    for step in TOUR_STEPS:
        console.clear()
        console.print(render_step(step))
        if auto_advance:
            continue
        try:
            response = input_func().strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print()
            console.print("[dim]tour exited.[/dim]")
            return 0
        if response in ("q", "quit", "exit"):
            console.print()
            console.print(
                "[dim]tour exited.  Try `aegis dashboard --demo` "
                "for a live preview.[/dim]",
            )
            return 0

    console.print()
    console.print(
        Align.center(
            Text(
                "✓  Tour 완료.  좋은 시작 되세요!",
                style="bold green",
            ),
        ),
    )
    console.print()
    return 0


# ── module CLI fallback ─────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    """Allow ``python -m aegis.tour`` invocation."""
    return run_tour()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
