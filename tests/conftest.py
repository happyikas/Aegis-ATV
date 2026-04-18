"""Shared pytest fixtures.

We force dummy embedding/judge providers so the test suite never reaches
out to OpenAI/Anthropic.
"""

from __future__ import annotations

import os

os.environ.setdefault("AEGIS_EMBEDDING_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_JUDGE_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_AUDIT_DB", ":memory:")
