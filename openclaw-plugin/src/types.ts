/**
 * Type definitions for Aegis ATV ↔ OpenClaw integration.
 *
 * The Aegis side schemas are mirrored here without depending on the
 * Aegis Python codebase — keeping the plugin a thin TS-only artifact
 * that publishes to npm without a Python build step.
 *
 * Source of truth (Python): src/aegis/schema.py + src/aegis/api/evaluate.py
 */

/**
 * OpenClaw `before_tool_call` event payload (subset Aegis cares about).
 * Full schema: https://docs.openclaw.ai/plugins/hooks.md
 */
export interface OpenClawBeforeToolCallEvent {
  /** Tool name (e.g. "shell", "fs.write", "kubectl"). */
  tool: string;
  /** Tool arguments as the agent invoked them. */
  params: Record<string, unknown>;
  /** Channel identifier — "telegram", "discord", "slack", "cli", "web". */
  channel?: string;
  /** Stable ID for the OpenClaw session this call belongs to. */
  sessionId?: string;
  /** Stable ID for this specific invocation (matches Aegis trace_id). */
  invocationId?: string;
  /** Identifier of the LLM provider that produced this tool_use intent. */
  provider?: string;
  /** Plain-text user-prompt that triggered the chain leading here. */
  userPrompt?: string;
  /** Recent assistant turns (for cumulative cost / cache trajectory). */
  recentTurns?: AssistantTurn[];
}

/**
 * One recent assistant turn — used by Aegis to compute cumulative
 * cost / cache_hit_rate trajectory and detect prefix mutations.
 */
export interface AssistantTurn {
  ts: string;                          // ISO-8601
  inputTokens?: number;
  outputTokens?: number;
  cacheReadInputTokens?: number;
  cacheCreationInputTokens?: number;
  /** Tool calls this turn made. */
  toolUses?: { name: string; input: Record<string, unknown> }[];
}

/**
 * OpenClaw `before_tool_call` return contract.
 * Source: https://docs.openclaw.ai/plugins/hooks.md#before-tool-call
 */
export type BeforeToolCallReturn =
  | undefined                          // no opinion → continue
  | { params: Record<string, unknown> } // ALLOW with rewritten args
  | { block: true; blockReason?: string } // BLOCK
  | {                                  // REQUIRE_APPROVAL
      requireApproval: {
        title: string;
        description: string;
        severity: "low" | "medium" | "high";
        timeoutMs?: number;
        timeoutBehavior?: "deny" | "approve";
      };
    };

/**
 * Aegis sidecar `/evaluate` request body. Direct mirror of
 * src/aegis/api/evaluate.py:EvaluateRequest.
 */
export interface AegisEvaluateRequest {
  tool_name: string;
  tool_input: Record<string, unknown>;
  /** ATV header — tenant_id is the multi-channel attribution dimension. */
  tenant_id: string;
  session_id?: string;
  invocation_id?: string;
  /** Optional: provider hint for ATV's provider-drift detection. */
  provider?: string;
  /** Optional: channel hint for per-channel baseline lookup. */
  channel?: string;
  /** Optional: recent turns for cumulative cost / cache trajectory. */
  recent_turns?: AssistantTurn[];
  /** Optional: user prompt for prompt-injection detection. */
  user_prompt?: string;
}

/**
 * Aegis sidecar `/evaluate` response body. Direct mirror of
 * src/aegis/api/evaluate.py:EvaluateResponse.
 */
export interface AegisEvaluateResponse {
  /** ALLOW / REQUIRE_APPROVAL / BLOCK. */
  decision: "ALLOW" | "REQUIRE_APPROVAL" | "BLOCK";
  reason: string;
  trace_id: string;
  /** When ALLOW with sanitized params (Aegis step312 normalize), the
   * sanitized `tool_input` is returned here for the plugin to apply. */
  sanitized_input?: Record<string, unknown>;
  /** Optional advisor recommendations (--profile pro/cloud only). */
  advisor_signals?: AdvisorSignal[];
  /** Optional severity for REQUIRE_APPROVAL routing. */
  severity?: "low" | "medium" | "high";
  /** Per-step trace map (key=step name, value=trace string). */
  step_traces?: Record<string, string>;
}

export interface AdvisorSignal {
  advisor: string;
  severity: "low" | "medium" | "high";
  message: string;
  trace_id?: string;
}

/**
 * Plugin runtime configuration. Loaded by OpenClaw from
 * `openclaw.plugin.json` and may be overridden per-install.
 */
export interface AegisPluginConfig {
  aegisUrl: string;
  tenantId: string;
  timeoutMs: number;
  failClosed: boolean;
}

export const DEFAULT_CONFIG: AegisPluginConfig = {
  aegisUrl: "http://localhost:8000",
  tenantId: "default",
  timeoutMs: 1500,
  failClosed: false,
};
