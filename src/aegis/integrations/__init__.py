"""Aegis integrations — adapters for upstream LLM gateways and
agent runtimes.

Each submodule is a thin, pure-Python helper. They DO NOT make
network calls; they only transform third-party response shapes
into Aegis's canonical fields (notably the ``provider`` string
that drives ``aegis report --by-provider`` cross-grouping).

Available adapters:

* :mod:`aegis.integrations.openrouter` — OpenRouter LLM gateway
  (300+ models, 60+ providers, fallback chains).

Adding a new adapter? Keep the contract:

1. Pure function or frozen dataclass — never reach for the network.
2. Produce a canonical ``provider`` string in the form
   ``"<gateway>:<vendor>-<model>"`` (lowercase, hyphen-separated)
   so existing ``aegis report --by-provider`` keeps working.
3. Surface the full fallback / divergence chain via a separate
   structured field so downstream consumers (advisor pipeline,
   cost report) can read it without re-parsing.
4. Unit tests under ``tests/unit/test_integrations_<name>.py``.
"""

from __future__ import annotations
