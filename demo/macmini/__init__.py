"""Aegis Mac mini validation suite — 90 cases across Cost / Performance / Security.

Self-contained, deterministic, dummy-provider-only. Designed to run on
a stock Mac mini with ``uv run python -m demo.macmini`` (no API keys,
no GPU, no docker required).

Public entry points:

* ``demo.macmini.cost.cases()``         — 30 Cost test cases.
* ``demo.macmini.performance.cases()``  — 30 Performance test cases.
* ``demo.macmini.security.cases()``     — 30 Security test cases.
* ``demo.macmini.runner.run(category)`` — drives the suite, returns results.
* ``python -m demo.macmini [cost|performance|security|all]`` — CLI.

The module is intentionally stdlib-only so it can serve as an
open-source reference implementation of how to exercise the v2.8
ActionStep surface end-to-end.
"""
from __future__ import annotations

__all__ = [
    "__version__",
]

__version__ = "1.0.0"
