"""Tests for ``aegis.tour`` — interactive onboarding."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from aegis.tour import TOUR_STEPS, run_tour
from aegis.tour.main import TOTAL_STEPS, render_step

# ── step data invariants ────────────────────────────────────────


def test_tour_has_seven_steps() -> None:
    """Tour 길이 변경은 의도적인 결정이어야 — total 변수가 단일 진실."""
    assert len(TOUR_STEPS) == TOTAL_STEPS == 7


def test_each_step_has_unique_number() -> None:
    numbers = [s.number for s in TOUR_STEPS]
    assert numbers == list(range(1, len(TOUR_STEPS) + 1))


def test_each_step_has_required_fields() -> None:
    """모든 step 이 title / eyebrow / body / next_hint 갖춤."""
    for step in TOUR_STEPS:
        assert step.title.strip(), f"step {step.number} missing title"
        assert step.eyebrow.strip(), f"step {step.number} missing eyebrow"
        assert step.body, f"step {step.number} missing body"
        assert step.next_hint.strip(), f"step {step.number} missing next_hint"


def test_eyebrows_announce_progress() -> None:
    """각 eyebrow 가 'STEP N OF 7' 형태로 진행도 알림."""
    for step in TOUR_STEPS:
        assert f"STEP {step.number} OF 7" in step.eyebrow


def test_last_step_mentions_dashboard_or_docs() -> None:
    """마지막 panel 은 사용자가 다음에 할 일을 알려야 — '다음 단계' UX 보장."""
    last = TOUR_STEPS[-1]
    body_text = "\n".join(last.body)
    assert "dashboard" in body_text.lower() or "USER_GUIDE" in body_text


def test_install_step_shows_install_command() -> None:
    """설치 panel 이 실제 install 명령어 포함."""
    install_steps = [s for s in TOUR_STEPS if "install" in s.title.lower() or "설치" in s.title]
    assert install_steps, "no install-themed step found"
    body_text = "\n".join(install_steps[0].body)
    assert "aegis install" in body_text


def test_metaphor_step_has_three_emojis() -> None:
    """비유 panel 이 자물쇠 · CCTV · 영수증 세 비유 모두 등장."""
    metaphor_steps = [
        s for s in TOUR_STEPS
        if "비유" in s.title or "concept" in s.eyebrow.lower()
    ]
    assert metaphor_steps, "no metaphor step"
    body_text = "\n".join(metaphor_steps[0].body)
    assert "🚪" in body_text and "📹" in body_text and "🧾" in body_text


# ── render_step ─────────────────────────────────────────────────


def test_render_step_returns_panel() -> None:
    from rich.panel import Panel
    step = TOUR_STEPS[0]
    panel = render_step(step)
    assert isinstance(panel, Panel)


def test_render_step_includes_title_and_body() -> None:
    """렌더링된 panel 텍스트에 title + 일부 body line 포함."""
    step = TOUR_STEPS[0]
    panel = render_step(step)
    buf = StringIO()
    Console(file=buf, width=100, force_terminal=False).print(panel)
    output = buf.getvalue()
    # Title rendered
    assert "Aegis" in output
    # Some body line rendered (just check Enter exit hint)
    assert "Enter" in output or "q" in output.lower()


def test_render_step_shows_progress_indicator() -> None:
    """panel 의 우상단에 N/7 진행도 표시."""
    step = TOUR_STEPS[2]  # step 3
    panel = render_step(step)
    buf = StringIO()
    Console(file=buf, width=100, force_terminal=False).print(panel)
    output = buf.getvalue()
    assert "3/7" in output


# ── run_tour ────────────────────────────────────────────────────


def test_run_tour_auto_advance_completes() -> None:
    """auto_advance=True 면 모든 step 출력 후 0 반환."""
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    rc = run_tour(console=console, auto_advance=True)
    assert rc == 0
    output = buf.getvalue()
    # All 7 steps rendered
    for step in TOUR_STEPS:
        assert step.title in output, f"step {step.number} not rendered"
    assert "완료" in output


def test_run_tour_quits_on_q() -> None:
    """사용자가 q 입력 시 즉시 종료."""
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    rc = run_tour(console=console, input_func=lambda: "q")
    assert rc == 0
    output = buf.getvalue()
    # Step 1 rendered but not step 7
    assert TOUR_STEPS[0].title in output
    assert TOUR_STEPS[-1].title not in output
    assert "exited" in output.lower() or "종료" in output


def test_run_tour_quits_on_quit_word() -> None:
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    rc = run_tour(console=console, input_func=lambda: "quit")
    assert rc == 0


def test_run_tour_handles_keyboard_interrupt() -> None:
    """Ctrl-C 가 깔끔하게 종료해야 (raise X)."""
    def _raise_kbi() -> str:
        raise KeyboardInterrupt
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    rc = run_tour(console=console, input_func=_raise_kbi)
    assert rc == 0


def test_run_tour_handles_eof() -> None:
    """EOFError (pipe / redirect from /dev/null) 처리."""
    def _raise_eof() -> str:
        raise EOFError
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    rc = run_tour(console=console, input_func=_raise_eof)
    assert rc == 0


def test_run_tour_advances_on_enter() -> None:
    """빈 입력 (Enter) 시 다음 step 으로 진행 — N step = N input() 호출."""
    inputs = iter([""] * TOTAL_STEPS)
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    rc = run_tour(console=console, input_func=lambda: next(inputs))
    assert rc == 0
    output = buf.getvalue()
    # All steps reached
    for step in TOUR_STEPS:
        assert step.title in output


# ── CLI wiring ──────────────────────────────────────────────────


def test_cli_tour_subcommand_wired() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # type: ignore[import-not-found]

    parser = aegis_cli.build_parser()
    args = parser.parse_args(["tour", "--auto"])
    assert args.fn is aegis_cli.cmd_tour
    assert args.auto is True


def test_cli_tour_default_is_interactive() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # type: ignore[import-not-found]

    parser = aegis_cli.build_parser()
    args = parser.parse_args(["tour"])
    assert args.auto is False
