"""ATV (Agent Trace Vector) schema — 2080-D, version ATV-2080-v1.

The slice constants below encode the index ranges of each band of the
2080-D vector. They are the canonical truth: every encoder and consumer
must use these slices, never hard-coded indices.

Hardware band (indices 1880..2080) is intentionally zero-filled in T2;
T3 builds will populate it from eBPF/iostat/CSD telemetry.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

ATV_VERSION = "ATV-2080-v1"
ATV_DIM = 2080

# --- Software band (0..1880) -----------------------------------------------
SLICE_HEADER = slice(0, 64)
SLICE_AGENT_STATE = slice(64, 576)        # 512-D
SLICE_PLAN = slice(576, 1088)             # 512-D
SLICE_TOOL_CALL = slice(1088, 1472)       # 384-D
SLICE_SAFETY_FLAGS = slice(1472, 1728)    # 256-D
SLICE_MEMORY_FP = slice(1728, 1864)       # 136-D
SLICE_COST_EFFICIENCY = slice(1864, 1880) # 16-D
# --- Hardware band (zero-filled in T2) -------------------------------------
SLICE_IO_PROFILE = slice(1880, 1960)
SLICE_DMA_FANOUT = slice(1960, 2040)
SLICE_HW_COST = slice(2040, 2060)
SLICE_LINKAGE = slice(2060, 2080)
SLICE_DIVERGENCE = slice(2057, 2060)


class ATVHeader(BaseModel):
    trace_id: str
    span_id: str
    tenant_id: str
    aid: str
    ats: str = ATV_VERSION
    timestamp_ns: int
    model_hash: str | None = None
    burn_in_id: str | None = None


class CostEfficiency(BaseModel):
    """16-D cost-efficiency band. 12 named fields + 4 reserved."""

    exp_bytes_read: float = 0.0
    exp_bytes_write: float = 0.0
    exp_iops: float = 0.0
    exp_time_ms: float = 0.0
    exp_net_in: float = 0.0
    exp_net_out: float = 0.0
    exp_tokens: float = 0.0
    exp_api_calls: float = 0.0
    exp_dollars: float = 0.0
    confidence: float = 1.0
    flag_high_risk: float = 0.0
    flag_batch: float = 0.0
    reserved: list[float] = Field(default_factory=lambda: [0.0] * 4)

    def to_array(self) -> np.ndarray:
        arr = np.array(
            [
                self.exp_bytes_read,
                self.exp_bytes_write,
                self.exp_iops,
                self.exp_time_ms,
                self.exp_net_in,
                self.exp_net_out,
                self.exp_tokens,
                self.exp_api_calls,
                self.exp_dollars,
                self.confidence,
                self.flag_high_risk,
                self.flag_batch,
                *self.reserved,
            ],
            dtype=np.float32,
        )
        if arr.size != 16:
            raise ValueError(f"CostEfficiency.to_array must return 16-D, got {arr.size}")
        return arr


class ATVInput(BaseModel):
    """Payload the agent posts to /evaluate."""

    header: ATVHeader
    agent_state_text: str
    plan_text: str
    tool_name: str
    tool_args_json: str
    safety_flags: dict[str, float] = Field(default_factory=dict)
    memory_fingerprint: str | None = None
    cost_estimate: CostEfficiency = Field(default_factory=CostEfficiency)


class Verdict(BaseModel):
    decision: Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"]
    reason: str
    atv_id: str
    signature: str | None = None
    confidence: float = 1.0
    step_traces: dict[str, str] = Field(default_factory=dict)
