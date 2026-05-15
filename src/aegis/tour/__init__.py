"""Aegis interactive tour — 60-second onboarding.

A guided rich-based walkthrough that explains Aegis in plain
language. Each step shows one panel; the user presses Enter to
advance or q to exit. Total reading time ~60s.

CLI entry: ``aegis tour`` (see :mod:`tools.aegis_cli`).
"""

from __future__ import annotations

from aegis.tour.main import (
    TOUR_STEPS,
    TourStep,
    run_tour,
)

__all__ = ["TOUR_STEPS", "TourStep", "run_tour"]
