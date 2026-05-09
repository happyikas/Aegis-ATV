/**
 * Thin HTTP client for the Aegis sidecar `/evaluate` endpoint.
 *
 * Has no dependency on a specific HTTP library — uses Node 20+ global
 * `fetch` and `AbortController`. That keeps the npm install footprint
 * tiny (only `@types/node` + `typescript` + `vitest` as dev deps).
 */

import type {
  AegisEvaluateRequest,
  AegisEvaluateResponse,
  AegisPluginConfig,
} from "./types.js";

/**
 * Errors that the plugin treats as "sidecar unreachable" and routes
 * through the failClosed policy.
 */
export class AegisSidecarError extends Error {
  constructor(
    message: string,
    public readonly cause?: unknown,
  ) {
    super(message);
    this.name = "AegisSidecarError";
  }
}

/**
 * POST a single evaluation request to the Aegis sidecar.
 *
 * Throws AegisSidecarError on:
 *   - Network failure (sidecar down, DNS, etc.)
 *   - Timeout (config.timeoutMs)
 *   - Non-2xx response from sidecar
 *
 * The plugin's caller decides what to do on error (fail-open vs
 * fail-closed) per `config.failClosed`.
 */
export async function evaluate(
  request: AegisEvaluateRequest,
  config: AegisPluginConfig,
  fetchImpl: typeof fetch = fetch,
): Promise<AegisEvaluateResponse> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.timeoutMs);

  try {
    const url = `${trimSlash(config.aegisUrl)}/evaluate`;
    const response = await fetchImpl(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(request),
      signal: controller.signal,
    });

    if (!response.ok) {
      const body = await safeReadText(response);
      throw new AegisSidecarError(
        `Aegis /evaluate returned ${response.status}: ${body.slice(0, 200)}`,
      );
    }

    const data = (await response.json()) as AegisEvaluateResponse;
    if (!isValidResponse(data)) {
      throw new AegisSidecarError(
        `Aegis /evaluate returned malformed body: ${JSON.stringify(data).slice(0, 200)}`,
      );
    }
    return data;
  } catch (err) {
    if (err instanceof AegisSidecarError) throw err;
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new AegisSidecarError(
        `Aegis /evaluate timed out after ${config.timeoutMs}ms`,
        err,
      );
    }
    throw new AegisSidecarError(
      `Aegis /evaluate failed: ${err instanceof Error ? err.message : String(err)}`,
      err,
    );
  } finally {
    clearTimeout(timer);
  }
}

function trimSlash(url: string): string {
  return url.endsWith("/") ? url.slice(0, -1) : url;
}

async function safeReadText(response: Response): Promise<string> {
  try {
    return await response.text();
  } catch {
    return "<unreadable>";
  }
}

function isValidResponse(data: unknown): data is AegisEvaluateResponse {
  if (data === null || typeof data !== "object") return false;
  const decision = (data as Record<string, unknown>).decision;
  return decision === "ALLOW" || decision === "REQUIRE_APPROVAL" || decision === "BLOCK";
}
