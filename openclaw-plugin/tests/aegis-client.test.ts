/**
 * Vitest tests for the Aegis HTTP client (low-level, separate from
 * the handler tests which exercise the full mapping path).
 */

import { describe, expect, it, vi } from "vitest";
import { AegisSidecarError, evaluate } from "../src/aegis-client.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import type { AegisEvaluateRequest } from "../src/types.js";

const request: AegisEvaluateRequest = {
  tool_name: "shell",
  tool_input: { command: "ls" },
  tenant_id: "default",
};

describe("evaluate()", () => {
  it("returns parsed body on 200", async () => {
    const fetchImpl = vi.fn(async () => {
      return new Response(
        JSON.stringify({ decision: "ALLOW", reason: "ok", trace_id: "t" }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    const response = await evaluate(request, DEFAULT_CONFIG, fetchImpl);
    expect(response.decision).toBe("ALLOW");
    expect(response.trace_id).toBe("t");
  });

  it("throws AegisSidecarError on 500", async () => {
    const fetchImpl = vi.fn(async () => {
      return new Response("kaboom", { status: 500 });
    }) as unknown as typeof fetch;

    await expect(evaluate(request, DEFAULT_CONFIG, fetchImpl)).rejects.toBeInstanceOf(
      AegisSidecarError,
    );
  });

  it("throws AegisSidecarError on network failure", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("ECONNREFUSED");
    }) as unknown as typeof fetch;

    await expect(evaluate(request, DEFAULT_CONFIG, fetchImpl)).rejects.toBeInstanceOf(
      AegisSidecarError,
    );
  });

  it("throws AegisSidecarError on malformed JSON body", async () => {
    const fetchImpl = vi.fn(async () => {
      return new Response("{not json", {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;

    await expect(evaluate(request, DEFAULT_CONFIG, fetchImpl)).rejects.toBeInstanceOf(
      AegisSidecarError,
    );
  });

  it("throws AegisSidecarError on unknown decision value", async () => {
    const fetchImpl = vi.fn(async () => {
      return new Response(
        JSON.stringify({ decision: "MAYBE", reason: "?", trace_id: "t" }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    await expect(evaluate(request, DEFAULT_CONFIG, fetchImpl)).rejects.toBeInstanceOf(
      AegisSidecarError,
    );
  });

  it("trims trailing slash from aegisUrl and posts to /evaluate/openclaw", async () => {
    const fetchImpl = vi.fn(async (url: string) => {
      // PR-D — plugin posts to the OpenClaw-specific adapter route,
      // not to /evaluate (which expects the full ATVInput shape).
      expect(url).toBe("http://localhost:8000/evaluate/openclaw");
      return new Response(
        JSON.stringify({ decision: "ALLOW", reason: "", trace_id: "t" }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    await evaluate(
      request,
      { ...DEFAULT_CONFIG, aegisUrl: "http://localhost:8000/" },
      fetchImpl,
    );
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it("aborts via AbortController on timeout", async () => {
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => {
      return new Promise<Response>((_, reject) => {
        const signal = init.signal as AbortSignal | undefined;
        signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    }) as unknown as typeof fetch;

    await expect(
      evaluate(request, { ...DEFAULT_CONFIG, timeoutMs: 25 }, fetchImpl),
    ).rejects.toThrowError(/timed out after 25ms/);
  });
});
