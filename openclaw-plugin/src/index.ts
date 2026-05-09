/**
 * @openclaw/plugin-aegis — entry point.
 *
 * Registers the Aegis ATV `before_tool_call` handler with the
 * OpenClaw plugin runtime. OpenClaw plugins receive an `api` object
 * exposing `.on(event, handler)` — Aegis only listens on
 * `"before_tool_call"`.
 *
 * Source-of-truth event spec:
 *   https://docs.openclaw.ai/plugins/hooks.md
 */

import { handleBeforeToolCall } from "./handler.js";
import { DEFAULT_CONFIG } from "./types.js";
import type {
  AegisPluginConfig,
  BeforeToolCallReturn,
  OpenClawBeforeToolCallEvent,
} from "./types.js";

/**
 * The OpenClaw `api` object passed to plugins on activation. Loose
 * type — we only use `.on()` and `.config()` (the rest of the
 * surface is plugin-author-specific and varies across OpenClaw
 * versions).
 */
export interface OpenClawPluginApi {
  on(
    event: "before_tool_call",
    handler: (event: OpenClawBeforeToolCallEvent) => Promise<BeforeToolCallReturn>,
  ): void;
  /** Plugin-scoped config from openclaw.plugin.json + per-install overrides. */
  config?(): Partial<AegisPluginConfig>;
}

/**
 * OpenClaw calls this default export when activating the plugin.
 *
 * Example openclaw user-code:
 *
 *   import { activate } from "@openclaw/plugin-aegis";
 *   activate(api);
 */
export function activate(api: OpenClawPluginApi): void {
  const userConfig = api.config?.() ?? {};
  const config: AegisPluginConfig = {
    ...DEFAULT_CONFIG,
    ...userConfig,
  };

  api.on("before_tool_call", async (event) => {
    return handleBeforeToolCall(event, { config });
  });
}

// Default export for OpenClaw runtimes that auto-call default(api).
export default activate;

// Re-export the public surface for users who want to wire things up
// manually (or run the handler in tests).
export { handleBeforeToolCall } from "./handler.js";
export { evaluate, AegisSidecarError } from "./aegis-client.js";
export type {
  AegisEvaluateRequest,
  AegisEvaluateResponse,
  AegisPluginConfig,
  AdvisorSignal,
  AssistantTurn,
  BeforeToolCallReturn,
  OpenClawBeforeToolCallEvent,
} from "./types.js";
export { DEFAULT_CONFIG } from "./types.js";
