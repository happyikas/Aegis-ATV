/**
 * `before_tool_call` handler — the heart of the plugin.
 *
 * Maps OpenClaw event → Aegis evaluate request → Aegis verdict →
 * OpenClaw return contract.
 *
 * Verdict mapping (Aegis decision → OpenClaw return):
 *   - ALLOW with sanitized_input  →  { params: <sanitized> }
 *   - ALLOW (no rewrite)          →  undefined (continue)
 *   - REQUIRE_APPROVAL            →  { requireApproval: {...} }
 *   - BLOCK                       →  { block: true, blockReason: <reason> }
 *
 * Errors (sidecar unreachable / timeout):
 *   - failClosed: false (default) →  undefined (continue) + warn
 *   - failClosed: true            →  { block: true, blockReason: "..." }
 */

import { AegisSidecarError, evaluate } from "./aegis-client.js";
import type {
  AegisEvaluateRequest,
  AegisEvaluateResponse,
  AegisPluginConfig,
  BeforeToolCallReturn,
  OpenClawBeforeToolCallEvent,
} from "./types.js";

export interface HandlerDeps {
  config: AegisPluginConfig;
  fetchImpl?: typeof fetch;
  /** Logger for sidecar errors / fail-open warnings. Defaults to console.warn. */
  warn?: (message: string) => void;
}

export async function handleBeforeToolCall(
  event: OpenClawBeforeToolCallEvent,
  deps: HandlerDeps,
): Promise<BeforeToolCallReturn> {
  const { config } = deps;
  const warn = deps.warn ?? ((m: string) => console.warn(`[aegis] ${m}`));

  const request: AegisEvaluateRequest = {
    tool_name: event.tool,
    tool_input: event.params ?? {},
    tenant_id: config.tenantId,
    session_id: event.sessionId,
    invocation_id: event.invocationId,
    provider: event.provider,
    channel: event.channel,
    user_prompt: event.userPrompt,
    recent_turns: event.recentTurns,
  };

  let response: AegisEvaluateResponse;
  try {
    response = await evaluate(request, config, deps.fetchImpl);
  } catch (err) {
    if (!(err instanceof AegisSidecarError)) throw err;
    warn(`sidecar error: ${err.message} — ${config.failClosed ? "BLOCKING (fail-closed)" : "continuing (fail-open)"}`);
    if (config.failClosed) {
      return {
        block: true,
        blockReason: `Aegis sidecar unreachable: ${err.message}`,
      };
    }
    return undefined;
  }

  return mapVerdict(response, event);
}

function mapVerdict(
  response: AegisEvaluateResponse,
  event: OpenClawBeforeToolCallEvent,
): BeforeToolCallReturn {
  switch (response.decision) {
    case "ALLOW":
      if (response.sanitized_input !== undefined) {
        return { params: response.sanitized_input };
      }
      return undefined;

    case "REQUIRE_APPROVAL":
      return {
        requireApproval: {
          title: `Aegis: ${event.tool} requires approval`,
          description: response.reason || "Aegis judged this operation as grey-zone.",
          severity: response.severity ?? "medium",
          timeoutMs: 60_000,
          timeoutBehavior: "deny",
        },
      };

    case "BLOCK":
      return {
        block: true,
        blockReason: response.reason || "Blocked by Aegis ATV firewall.",
      };
  }
}
