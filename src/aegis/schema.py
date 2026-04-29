"""ATV-2080-v1 schema, conformant to AegisData provisional patent v7.10
(Appendix A — Agent Telemetry Vector Schema Index Map).

The 2,080-element float32 tensor is partitioned into 30 subfields with
fixed index assignments:

    SW band  (0..1879, 1880-D, 19 subfields)
        0    .. 767   agent_state_embedding         768
        768  .. 1407  action_history                640
        1408 .. 1535  inter_agent_graph             128
        1536 .. 1599  memory_provenance              64
        1600 .. 1615  qom_scores                     16
        1616 .. 1647  resource_access_pattern        32
        1648 .. 1663  prompt_structure               16
        1664 .. 1671  aid_ats_scalars                 8
        1672 .. 1683  encryption_metadata            12
        1684 .. 1747  output_content_fingerprint     64
        1748 .. 1779  tool_arg_inspection            32
        1780 .. 1795  action_blast_radius            16
        1796 .. 1807  output_channel_diversity       12
        1808 .. 1823  session_behavioral_drift       16
        1824 .. 1835  mcp_trust_signals              12
        1836 .. 1851  grounding_metrics              16
        1852 .. 1855  novelty_score                   4
        1856 .. 1863  human_oversight_state           8
        1864 .. 1879  cost_efficiency_metrics        16

    HW band  (1880..2079, 200-D, 11 subfields — T2 zero-filled)
        1880 .. 1911  memory_timing_histograms       32
        1912 .. 1935  aid_tag_transitions            24
        1936 .. 1951  atmu_anomaly                   16    (ATMU = Agent Telemetry Management Unit)
        1952 .. 1967  dma_fanout                     16
        1968 .. 1983  thermal_ecc_drift              16
        1984 .. 1995  watchdog_signals               12
        1996 .. 2019  network_telemetry              24
        2020 .. 2035  gpu_accelerator_state          16
        2036 .. 2043  hypervisor_signals              8
        2044 .. 2059  hw_cost_attestation            16
        2060 .. 2079  linkage_consistency_features   20
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

ATV_VERSION = "ATV-2080-v1"
ATV_DIM = 2080

# ─────────────────────────────────────────────────────────────────────
# Subfield slice constants — patent Appendix A
# ─────────────────────────────────────────────────────────────────────
# SW band
SLICE_AGENT_STATE_EMBEDDING       = slice(0,    768)   # 768
SLICE_ACTION_HISTORY              = slice(768,  1408)  # 640
SLICE_INTER_AGENT_GRAPH           = slice(1408, 1536)  # 128
SLICE_MEMORY_PROVENANCE           = slice(1536, 1600)  # 64
SLICE_QOM_SCORES                  = slice(1600, 1616)  # 16
SLICE_RESOURCE_ACCESS_PATTERN     = slice(1616, 1648)  # 32
SLICE_PROMPT_STRUCTURE            = slice(1648, 1664)  # 16
SLICE_AID_ATS_SCALARS             = slice(1664, 1672)  # 8
SLICE_ENCRYPTION_METADATA         = slice(1672, 1684)  # 12
SLICE_OUTPUT_CONTENT_FINGERPRINT  = slice(1684, 1748)  # 64
SLICE_TOOL_ARG_INSPECTION         = slice(1748, 1780)  # 32
SLICE_ACTION_BLAST_RADIUS         = slice(1780, 1796)  # 16
SLICE_OUTPUT_CHANNEL_DIVERSITY    = slice(1796, 1808)  # 12
SLICE_SESSION_BEHAVIORAL_DRIFT    = slice(1808, 1824)  # 16
SLICE_MCP_TRUST_SIGNALS           = slice(1824, 1836)  # 12
SLICE_GROUNDING_METRICS           = slice(1836, 1852)  # 16
SLICE_NOVELTY_SCORE               = slice(1852, 1856)  # 4
SLICE_HUMAN_OVERSIGHT_STATE       = slice(1856, 1864)  # 8
SLICE_COST_EFFICIENCY_METRICS     = slice(1864, 1880)  # 16
# HW band
SLICE_MEMORY_TIMING_HISTOGRAMS    = slice(1880, 1912)  # 32
SLICE_AID_TAG_TRANSITIONS         = slice(1912, 1936)  # 24
SLICE_ATMU_ANOMALY                = slice(1936, 1952)  # 16
SLICE_DMA_FANOUT                  = slice(1952, 1968)  # 16
SLICE_THERMAL_ECC_DRIFT           = slice(1968, 1984)  # 16
SLICE_WATCHDOG_SIGNALS            = slice(1984, 1996)  # 12
SLICE_NETWORK_TELEMETRY           = slice(1996, 2020)  # 24
SLICE_GPU_ACCELERATOR_STATE       = slice(2020, 2036)  # 16
SLICE_HYPERVISOR_SIGNALS          = slice(2036, 2044)  # 8
SLICE_HW_COST_ATTESTATION         = slice(2044, 2060)  # 16
SLICE_LINKAGE_CONSISTENCY         = slice(2060, 2080)  # 20

# Composite spans for convenience
SLICE_SW_BAND = slice(0, 1880)
SLICE_HW_BAND = slice(1880, 2080)

ALL_SUBFIELDS: list[tuple[str, slice]] = [
    # SW
    ("agent_state_embedding",       SLICE_AGENT_STATE_EMBEDDING),
    ("action_history",              SLICE_ACTION_HISTORY),
    ("inter_agent_graph",           SLICE_INTER_AGENT_GRAPH),
    ("memory_provenance",           SLICE_MEMORY_PROVENANCE),
    ("qom_scores",                  SLICE_QOM_SCORES),
    ("resource_access_pattern",     SLICE_RESOURCE_ACCESS_PATTERN),
    ("prompt_structure",            SLICE_PROMPT_STRUCTURE),
    ("aid_ats_scalars",             SLICE_AID_ATS_SCALARS),
    ("encryption_metadata",         SLICE_ENCRYPTION_METADATA),
    ("output_content_fingerprint",  SLICE_OUTPUT_CONTENT_FINGERPRINT),
    ("tool_arg_inspection",         SLICE_TOOL_ARG_INSPECTION),
    ("action_blast_radius",         SLICE_ACTION_BLAST_RADIUS),
    ("output_channel_diversity",    SLICE_OUTPUT_CHANNEL_DIVERSITY),
    ("session_behavioral_drift",    SLICE_SESSION_BEHAVIORAL_DRIFT),
    ("mcp_trust_signals",           SLICE_MCP_TRUST_SIGNALS),
    ("grounding_metrics",           SLICE_GROUNDING_METRICS),
    ("novelty_score",               SLICE_NOVELTY_SCORE),
    ("human_oversight_state",       SLICE_HUMAN_OVERSIGHT_STATE),
    ("cost_efficiency_metrics",     SLICE_COST_EFFICIENCY_METRICS),
    # HW
    ("memory_timing_histograms",    SLICE_MEMORY_TIMING_HISTOGRAMS),
    ("aid_tag_transitions",         SLICE_AID_TAG_TRANSITIONS),
    ("atmu_anomaly",                SLICE_ATMU_ANOMALY),
    ("dma_fanout",                  SLICE_DMA_FANOUT),
    ("thermal_ecc_drift",           SLICE_THERMAL_ECC_DRIFT),
    ("watchdog_signals",            SLICE_WATCHDOG_SIGNALS),
    ("network_telemetry",           SLICE_NETWORK_TELEMETRY),
    ("gpu_accelerator_state",       SLICE_GPU_ACCELERATOR_STATE),
    ("hypervisor_signals",          SLICE_HYPERVISOR_SIGNALS),
    ("hw_cost_attestation",         SLICE_HW_COST_ATTESTATION),
    ("linkage_consistency_features",SLICE_LINKAGE_CONSISTENCY),
]


# ─────────────────────────────────────────────────────────────────────
# Header — patent ¶[0049]
# ─────────────────────────────────────────────────────────────────────
class ATVHeader(BaseModel):
    """Structured header accompanying every ATV. Patent ¶[0049] fields."""

    model_config = ConfigDict(populate_by_name=True)

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    tenant_id: str
    aid: str
    ats: str = ATV_VERSION                       # legacy alias for schema_version
    schema_version: str = ATV_VERSION
    timestamp_ns: int
    node_id: str | None = None
    pod_id: str | None = None
    tier_profile: Literal["T2", "T3"] = "T2"
    cost_attestation_profile: Literal["software", "hardware", "both"] = "software"
    model_hash: str | None = None
    burn_in_id: str | None = None
    atv_hash: str | None = None  # SHA3-256 of tensor; populated by signer

    # Aliases for legacy callers
    @property
    def ats_alias(self) -> str:
        return self.schema_version


# ─────────────────────────────────────────────────────────────────────
# Cost — 16-D cost_efficiency_metrics (s-1..s-16) per ¶[0045]
# ─────────────────────────────────────────────────────────────────────
class CostEfficiencyMetrics(BaseModel):
    """Patent ¶[0045] cost_efficiency_metrics subfield (16 dimensions).

    Indices s-1 through s-16 map directly to slots 0..15 within the 16-D
    cost_efficiency_metrics subfield at ATV indices 1864..1879.
    """

    # current step
    input_token_count: float = 0.0                       # s-1, idx 0
    output_token_count: float = 0.0                      # s-2, idx 1
    reasoning_token_count: float = 0.0                   # s-3, idx 2
    # cumulative across trace
    cumulative_tokens: float = 0.0                       # s-4, idx 3
    cumulative_dollars: float = 0.0                      # s-5, idx 4
    # efficiency ratios
    tokens_per_successful_tool_invocation: float = 0.0   # s-6, idx 5
    tokens_per_plan_step_completed: float = 0.0          # s-7, idx 6
    tokens_per_byte_of_final_output: float = 0.0         # s-8, idx 7
    reasoning_to_action_ratio: float = 0.0               # s-9, idx 8
    cache_hit_rate: float = 0.0                          # s-10, idx 9
    context_utilization_ratio: float = 0.0               # s-11, idx 10
    # baseline & forecast
    cost_delta_vs_role_baseline: float = 0.0             # s-12, idx 11
    budget_burn_rate: float = 0.0                        # s-13, idx 12
    forecasted_cost_to_completion: float = 0.0           # s-14, idx 13
    task_progress_score: float = 0.0                     # s-15, idx 14  (0..1)
    marginal_value_score: float = 0.0                    # s-16, idx 15

    def to_array(self) -> np.ndarray:
        arr = np.array(
            [
                self.input_token_count,
                self.output_token_count,
                self.reasoning_token_count,
                self.cumulative_tokens,
                self.cumulative_dollars,
                self.tokens_per_successful_tool_invocation,
                self.tokens_per_plan_step_completed,
                self.tokens_per_byte_of_final_output,
                self.reasoning_to_action_ratio,
                self.cache_hit_rate,
                self.context_utilization_ratio,
                self.cost_delta_vs_role_baseline,
                self.budget_burn_rate,
                self.forecasted_cost_to_completion,
                self.task_progress_score,
                self.marginal_value_score,
            ],
            dtype=np.float32,
        )
        if arr.size != 16:
            raise ValueError(f"cost_efficiency_metrics must be 16-D, got {arr.size}")
        return arr


# ─────────────────────────────────────────────────────────────────────
# ATVInput — what the host posts to /evaluate
# ─────────────────────────────────────────────────────────────────────
class ATVInput(BaseModel):
    """Host-posted bundle that Aegis converts into a 2080-D ATV.

    For T2 MVP, fields beyond the original demo set are optional and
    default to neutral values; encoders zero or deterministically hash
    bands for which the host hasn't supplied data.
    """

    model_config = ConfigDict(populate_by_name=True)

    header: ATVHeader

    # SW-band primary inputs
    agent_state_text: str = ""
    role_id: str | None = None
    capability_manifest: list[str] = Field(default_factory=list)

    plan_text: str = ""

    tool_name: str
    tool_args_json: str

    # used for action_history, inter_agent_graph encoders
    recent_actions: list[dict[str, Any]] = Field(default_factory=list)
    inter_agent_edges: list[tuple[str, str]] = Field(default_factory=list)

    # safety / behavioral
    safety_flags: dict[str, float] = Field(default_factory=dict)
    output_text: str = ""                             # last assistant output, for output_content_fingerprint
    session_behavior: dict[str, float] = Field(default_factory=dict)
    mcp_context: dict[str, float] = Field(default_factory=dict)
    grounding: dict[str, float] = Field(default_factory=dict)
    novelty: dict[str, float] = Field(default_factory=dict)
    oversight: dict[str, float] = Field(default_factory=dict)
    encryption_meta: dict[str, float] = Field(default_factory=dict)

    # provenance
    memory_fingerprint: str | None = None
    qom: dict[str, float] = Field(default_factory=dict)

    # cost
    cost_estimate: CostEfficiencyMetrics = Field(default_factory=CostEfficiencyMetrics)


# ─────────────────────────────────────────────────────────────────────
# Verdict
# ─────────────────────────────────────────────────────────────────────
class Verdict(BaseModel):
    decision: Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"]
    reason: str
    atv_id: str
    signature: str | None = None
    confidence: float = 1.0
    step_traces: dict[str, str] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# Sanity check that the schema is internally consistent
# ─────────────────────────────────────────────────────────────────────
def _assert_schema_valid() -> None:
    # contiguous + total == ATV_DIM
    expected = 0
    for name, sl in ALL_SUBFIELDS:
        if sl.start != expected:
            raise AssertionError(f"non-contiguous schema at {name}: expected {expected}, got {sl.start}")
        expected = sl.stop
    if expected != ATV_DIM:
        raise AssertionError(f"schema total {expected} != ATV_DIM {ATV_DIM}")


_assert_schema_valid()
