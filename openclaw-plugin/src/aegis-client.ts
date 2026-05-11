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
    // PR-D — POST to the OpenClaw-specific adapter route. Aegis
    // sidecar's `/evaluate` accepts the full ATVInput shape (used by
    // sidecar-internal callers); `/evaluate/openclaw` accepts this
    // plugin's flat request shape and builds ATVInput on the server
    // side. Keeping these as separate routes means we never have to
    // teach the plugin about Aegis's 30-subfield internal schema.
    const url = `${trimSlash(config.aegisUrl)}/evaluate/openclaw`;
    const response = await fetchImpl(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(request),
      signal: controller.signal,
    });

    if (!response.ok) {
      const body = await safeReadText(response);
      // PR-I (Gap 9) — surface a clear hint when the sidecar is too
      // old to know the /evaluate/openclaw route. The plugin (>= 0.2.0)
      // requires aegis-mvp >= 0.2.0; an older sidecar returns 404
      // (route not registered) and would otherwise silently fail-open,
      // which is exactly the wrong behaviour for a security plugin.
      const isOldSidecar404 =
        response.status === 404 && url.endsWith("/evaluate/openclaw");
      const hint = isOldSidecar404
        ? " — sidecar may be too old; this plugin (>= 0.2.0) " +
          "requires aegis-mvp >= 0.2.0 (which adds the " +
          "/evaluate/openclaw route). Run `pip install --upgrade " +
          "aegis-mvp` and restart the sidecar."
        : "";
      throw new AegisSidecarError(
        `Aegis /evaluate returned ${response.status}: ` +
          `${body.slice(0, 200)}${hint}`,
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
