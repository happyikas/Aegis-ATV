/**
 * Vitest tests for the `before_tool_call` handler.
 *
 * The Aegis sidecar is mocked at the fetch boundary — these tests
 * verify the *plugin-side* mapping logic only. End-to-end against a
 * real sidecar is covered by a separate integration test (not yet
 * written, gated on the OpenClaw runtime being available).
 */

import { describe, expect, it, vi } from "vitest";
import { handleBeforeToolCall } from "../src/handler.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import type {
  AegisEvaluateResponse,
  AegisPluginConfig,
  OpenClawBeforeToolCallEvent,
} from "../src/types.js";

function makeEvent(
  overrides: Partial<OpenClawBeforeToolCallEvent> = {},
): OpenClawBeforeToolCallEvent {
  return {
    tool: "shell",
    params: { command: "ls -la" },
    sessionId: "sess-test",
    invocationId: "inv-test",
    channel: "cli",
    provider: "anthropic-claude-3-5-sonnet",
    ...overrides,
  };
}

function makeFetchReturning(
  body: AegisEvaluateResponse,
  status = 200,
): typeof fetch {
  return vi.fn(async () => {
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  }) as unknown as typeof fetch;
}

const config: AegisPluginConfig = { ...DEFAULT_CONFIG };

describe("handleBeforeToolCall — verdict mapping", () => {
  it("ALLOW with no rewrite returns undefined (continue)", async () => {
    const fetchImpl = makeFetchReturning({
      decision: "ALLOW",
      reason: "ok",
      trace_id: "trace-1",
    });
    const result = await handleBeforeToolCall(makeEvent(), { config, fetchImpl });
    expect(result).toBeUndefined();
  });

  it("ALLOW with sanitized_input returns { params }", async () => {
    const fetchImpl = makeFetchReturning({
      decision: "ALLOW",
      reason: "redacted secret",
      trace_id: "trace-2",
      sanitized_input: { command: "ls -la" }, // pretend secret was stripped
    });
    const result = await handleBeforeToolCall(makeEvent(), { config, fetchImpl });
    expect(result).toEqual({ params: { command: "ls -la" } });
  });

  it("BLOCK returns { block: true, blockReason }", async () => {
    const fetchImpl = makeFetchReturning({
      decision: "BLOCK",
      reason: "rule:cloud_destructive (kubectl delete)",
      trace_id: "trace-3",
    });
    const result = await handleBeforeToolCall(
      makeEvent({ tool: "shell", params: { command: "kubectl delete ns prod" } }),
      { config, fetchImpl },
    );
    expect(result).toEqual({
      block: true,
      blockReason: "rule:cloud_destructive (kubectl delete)",
    });
  });

  it("REQUIRE_APPROVAL returns OpenClaw approval contract", async () => {
    const fetchImpl = makeFetchReturning({
      decision: "REQUIRE_APPROVAL",
      reason: "step336: same call repeated 3x",
      trace_id: "trace-4",
      severity: "high",
    });
    const result = await handleBeforeToolCall(
      makeEvent({ tool: "fs.write" }),
      { config, fetchImpl },
    );
    expect(result).toMatchObject({
      requireApproval: {
        title: "Aegis: fs.write requires approval",
        description: "step336: same call repeated 3x",
        severity: "high",
        timeoutMs: 60_000,
        timeoutBehavior: "deny",
      },
    });
  });

  it("REQUIRE_APPROVAL without severity defaults to medium", async () => {
    const fetchImpl = makeFetchReturning({
      decision: "REQUIRE_APPROVAL",
      reason: "step330: human gate",
      trace_id: "trace-5",
    });
    const result = await handleBeforeToolCall(makeEvent(), { config, fetchImpl });
    expect(result).toMatchObject({
      requireApproval: { severity: "medium" },
    });
  });

  it("BLOCK without reason still returns a usable blockReason", async () => {
    const fetchImpl = makeFetchReturning({
      decision: "BLOCK",
      reason: "",
      trace_id: "trace-6",
    });
    const result = await handleBeforeToolCall(makeEvent(), { config, fetchImpl });
    expect(result).toMatchObject({
      block: true,
      blockReason: "Blocked by Aegis ATV firewall.",
    });
  });
});

describe("handleBeforeToolCall — request shape", () => {
  it("forwards channel + provider + recent_turns to Aegis", async () => {
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string);
      expect(body.channel).toBe("telegram");
      expect(body.provider).toBe("openai-gpt-4o");
      expect(body.recent_turns).toHaveLength(1);
      return new Response(
        JSON.stringify({ decision: "ALLOW", reason: "", trace_id: "t" }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    await handleBeforeToolCall(
      makeEvent({
        channel: "telegram",
        provider: "openai-gpt-4o",
        recentTurns: [
          {
            ts: "2026-05-09T00:00:00Z",
            inputTokens: 100,
            cacheReadInputTokens: 80,
          },
        ],
      }),
      { config, fetchImpl },
    );

    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it("uses tenantId from config (not from event)", async () => {
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string);
      expect(body.tenant_id).toBe("acme-corp");
      return new Response(
        JSON.stringify({ decision: "ALLOW", reason: "", trace_id: "t" }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    await handleBeforeToolCall(makeEvent(), {
      config: { ...config, tenantId: "acme-corp" },
      fetchImpl,
    });

    expect(fetchImpl).toHaveBeenCalledOnce();
  });
});

describe("handleBeforeToolCall — sidecar errors", () => {
  it("fail-open by default: sidecar 500 returns undefined + warns", async () => {
    const fetchImpl = vi.fn(async () => {
      return new Response("internal", { status: 500 });
    }) as unknown as typeof fetch;
    const warn = vi.fn();

    const result = await handleBeforeToolCall(makeEvent(), {
      config,
      fetchImpl,
      warn,
    });

    expect(result).toBeUndefined();
    expect(warn).toHaveBeenCalledOnce();
    expect(warn.mock.calls[0][0]).toMatch(/continuing \(fail-open\)/);
  });

  it("fail-closed: sidecar 500 returns BLOCK", async () => {
    const fetchImpl = vi.fn(async () => {
      return new Response("internal", { status: 500 });
    }) as unknown as typeof fetch;
    const warn = vi.fn();

    const result = await handleBeforeToolCall(makeEvent(), {
      config: { ...config, failClosed: true },
      fetchImpl,
      warn,
    });

    expect(result).toMatchObject({
      block: true,
    });
    expect((result as { blockReason?: string }).blockReason).toMatch(/sidecar unreachable/i);
  });

  it("fail-open: sidecar timeout returns undefined", async () => {
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => {
      return new Promise<Response>((_, reject) => {
        const signal = init.signal as AbortSignal | undefined;
        signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    }) as unknown as typeof fetch;
    const warn = vi.fn();

    const result = await handleBeforeToolCall(makeEvent(), {
      config: { ...config, timeoutMs: 50 },
      fetchImpl,
      warn,
    });

    expect(result).toBeUndefined();
    expect(warn.mock.calls[0][0]).toMatch(/timed out/i);
  });

  it("malformed sidecar response is treated as sidecar error", async () => {
    const fetchImpl = vi.fn(async () => {
      return new Response(JSON.stringify({ decision: "MAYBE" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    const warn = vi.fn();

    const result = await handleBeforeToolCall(makeEvent(), {
      config,
      fetchImpl,
      warn,
    });

    // Default config is fail-open, so malformed = continue + warn.
    expect(result).toBeUndefined();
    expect(warn).toHaveBeenCalledOnce();
  });
});
