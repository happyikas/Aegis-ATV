"""Aegis Console TUI dashboard.

Single-screen rich-based dashboard that consolidates Coach / Live /
Doctor surfaces into one auto-refreshing view. Reads ContextMemory
and renders cost · performance · security panels + advisor
recommendations + recent BLOCKs.

CLI entry: ``aegis dashboard`` (see :mod:`tools.aegis_cli`).
"""

from __future__ import annotations

from aegis.dashboard.main import (
    build_layout,
    collect_stats,
    run_dashboard,
)

__all__ = ["build_layout", "collect_stats", "run_dashboard"]
