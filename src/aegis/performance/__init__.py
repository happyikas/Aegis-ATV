"""Aegis Performance Advisory Surface (v3.1+).

Out-of-band hint generators that read the same 2080-D ATV used by the
trust firewall and emit advisory data structures consumable by an
LLM serving runtime (vLLM / MLX-LM / llama.cpp / SGLang).

Patent linkage
--------------
The performance heads share the M13 attribution architecture (Claim 8)
but emit a *different* output type: instead of a 3-class verdict, a
KVCacheAdvice / SchedulingAdvice / PlacementAdvice payload. Both heads
are pure functions of the ATV — no model code modification is
required at the runtime layer; the runtime simply consults a hint.

v3.1 ships ``kv_cache_advisor`` only. v3.4 adds scheduling +
placement, v3.6 unifies them under one M13-extended head.
"""

from __future__ import annotations

from aegis.performance.context_advisor import (
    ContextAdvice,
    TurnAdvice,
    context_advisor,
)
from aegis.performance.feedback import (
    PerfFeedback,
    PerfFeedbackStore,
    get_default_store,
    reset_default_store,
)
from aegis.performance.kv_cache_advisor import (
    KVCacheAdvice,
    kv_cache_advisor,
)
from aegis.performance.placement_advisor import (
    PlacementAdvice,
    placement_advisor,
)
from aegis.performance.scheduling_advisor import (
    SchedulingAdvice,
    scheduling_advisor,
)

__all__ = [
    "ContextAdvice",
    "KVCacheAdvice",
    "PerfFeedback",
    "PerfFeedbackStore",
    "PlacementAdvice",
    "SchedulingAdvice",
    "TurnAdvice",
    "context_advisor",
    "get_default_store",
    "kv_cache_advisor",
    "placement_advisor",
    "reset_default_store",
    "scheduling_advisor",
]
