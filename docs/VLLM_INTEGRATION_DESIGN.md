# vLLM ↔ Aegis Integration Design (v3.5)

**Status:** design + reference shim shipped. End-to-end vLLM compile/test
deferred (vLLM not installed in this repo).

**Audience:** vLLM contributor or platform team integrating Aegis advisory.

---

## 1. Goal

Wire Aegis's advisory surface (`/advisory/kv_cache`, `/advisory/scheduling`,
`/advisory/placement`, or the combined `/advisory/all`) into a running
vLLM serving instance so the BlockManager and Scheduler can use ATV-based
hints **without modifying vLLM core logic**.

The integration is *advisory-only*: vLLM remains the enforcer with full
fallback to its native heuristics whenever Aegis is unreachable, slow,
or returns low confidence.

---

## 2. Three plug points

### 2.1 `AegisAwareBlockManager`

Subclass `vllm.core.block_manager.BlockManager`. Override:

```python
class AegisAwareBlockManager(BlockManager):
    def __init__(self, *args, advisor: VLLMAegisAdvisor, **kwargs):
        super().__init__(*args, **kwargs)
        self._advisor = advisor
        self._pin_set: set[str] = set()
        self._evict_priority: list[str] = []

    def apply_advice(self, advice: VLLMAdvice) -> None:
        """Called by the scheduler hook before allocate/swap decisions."""
        self._pin_set = set(advice.pin_block_ids)
        self._evict_priority = list(advice.evict_priority_block_ids)

    def _evict_block_internal(self, *args, **kwargs):
        # Try advice-named blocks first
        if self._evict_priority:
            target = self._evict_priority.pop(0)
            if target in self._block_table:
                return self._evict_specific(target)
        return super()._evict_block_internal(*args, **kwargs)

    def can_evict(self, block_id: str) -> bool:
        return block_id not in self._pin_set and super().can_evict(block_id)
```

The block-id namespace mismatch (Aegis emits `mem-XXXXXX` strings, vLLM
uses int block IDs) is bridged by a small lookup table maintained by
the prefetcher (§2.3).

### 2.2 `AegisAwareScheduler`

Subclass `vllm.core.scheduler.Scheduler`. Override `schedule()` to:

1. Group `SequenceGroup`s by `advice.cohort`.
2. Honour `priority_class` ordering: interactive > batch > low.
3. Drop a request to the next scheduling iteration if its
   `deadline_ms` budget is exhausted (graceful degradation rather than
   serving with a stale advisory).

```python
def schedule(self) -> SchedulerOutputs:
    groups_by_cohort: dict[str, list[SequenceGroup]] = defaultdict(list)
    for sg in self.waiting_seqs:
        advice = sg.metrics.get("aegis_advice")
        groups_by_cohort[advice.cohort if advice else ""].append(sg)

    # interactive cohorts first, batch second, low last
    ordered = sorted(
        groups_by_cohort.items(),
        key=lambda kv: _priority_score(kv[1]),
    )
    return self._build_outputs(ordered)
```

### 2.3 `AegisAwarePrefetcher`

A background `asyncio.Task` that:
- Walks active `SequenceGroup`s every ~50ms.
- For groups whose ATV has changed (next tool call, plan turn), POSTs
  to `/advisory/all`.
- Triggers async H2D copies for `pin_block_ids` ahead of decode.
- Closes the loop after each tool turn by POSTing measured perf
  (`cache_hit_rate`, `tokens_per_second`) to `/tool-outcome`.

```python
class AegisAwarePrefetcher:
    async def run(self) -> None:
        while not self._stop:
            for sg in self.engine.scheduler.iter_running():
                if self._needs_refresh(sg):
                    advice = await self.advisor.advise_async(self._build_atv(sg))
                    sg.metrics["aegis_advice"] = advice
                    self.block_mgr.apply_advice(advice)
                    self._kick_async_h2d(advice.pin_block_ids)
            await asyncio.sleep(0.050)
```

---

## 3. OpenAI-protocol middleware

Aegis lives in front of vLLM as a sidecar. The simplest deployment:

```
client ──► Aegis HTTP middleware ──► vLLM
              │                        │
              ▼                        │
       /advisory/all  ──────────────► used by Scheduler/BlockMgr
              ▲                        │
              │                        ▼
       /tool-outcome ◄──────── runtime metrics
```

The middleware:
- Reads `tenant_id` + `aid` from a custom header (`X-Aegis-Tenant`,
  `X-Aegis-Aid`) or the OpenAI `user` field.
- Tokenises the prompt to estimate `cumulative_tokens`, fills cost band.
- Forwards to vLLM with the `aegis_advice` payload attached as
  `extra_body`.

Reference middleware: ~150 LOC of FastAPI. Will be shipped as
`integrations/vllm/middleware.py` once vLLM is on the PYTHONPATH.

---

## 4. Failure modes & fallbacks

| Condition | Aegis response | vLLM behaviour |
|---|---|---|
| `/advisory/all` 5xx or timeout | `_project_to_vllm` returns confidence=0 | Skip advice, native scheduler |
| `confidence < 0.30` | Advice returned but flagged | Native scheduler, log warning |
| `pin_block_ids` reference unknown blocks | Lookup table miss | Treat as no-op |
| Network partition for ≥30 s | Advisor disabled by client-side breaker | Native scheduler |

**Latency budget**: Aegis `/advisory/all` is ≤5 ms p99 (sub-ms typical).
A vLLM scheduler tick is ~1-2 ms. The advisor call is therefore done
asynchronously by the prefetcher (§2.3) so it never blocks the schedule
loop.

---

## 5. Data flow diagram

```
                     ┌──────────────────────────────┐
                     │      Aegis sidecar           │
                     │                              │
                     │  POST /advisory/all          │
                     │   ├─ kv_cache_advisor        │
                     │   ├─ scheduling_advisor      │
                     │   └─ placement_advisor       │
                     │                              │
                     │  POST /tool-outcome          │
                     │   └─ EWMA feedback store     │
                     └────────┬─────────────────────┘
                              │ (HTTP, ≤5ms p99)
                              │
        ┌─────────────────────▼─────────────────────────────┐
        │                  vLLM engine                      │
        │                                                   │
        │  AegisAwarePrefetcher (asyncio task)              │
        │       │                                           │
        │       ▼                                           │
        │  AegisAwareScheduler  ─►  AegisAwareBlockManager  │
        │       │                            │              │
        │       └────► PagedAttention   ◄────┘              │
        └────────────────────────────────────────────────────┘
```

---

## 6. Roll-out plan

1. **Standalone shim verified** (this v3.5 commit) — `VLLMAegisAdvisor`,
   tests with mocked HTTP.
2. **vLLM dev environment**: stand up vLLM + a tiny model, build the
   `AegisAwareBlockManager` against vLLM's actual API.
3. **Benchmarks**: measure cache_hit_rate delta on a multi-tenant
   workload (MMLU + LongBench mix, mock multi-agent client) with and
   without Aegis advisory.
4. **vLLM PR**: upstream proposal for a `BlockManager` extension hook
   (so the override doesn't fork core).
5. **Patent claim filing**: Section 4 of the supplementary patent
   document covers the advisor → enforcer protocol.

---

## 7. References

- vLLM source: <https://github.com/vllm-project/vllm>
- vLLM PagedAttention: <https://docs.vllm.ai/en/latest/dev/paged_attention.html>
- This repo:
  - `integrations/vllm/__init__.py` — `VLLMAegisAdvisor` shim
  - `src/aegis/api/advisory.py` — `/advisory/all` endpoint
  - `tests/unit/test_runtime_adapters.py` — adapter tests (extend with vLLM tests)
