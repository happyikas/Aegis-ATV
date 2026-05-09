/**
 * E2E vitest config. Only picks up `tests/e2e/**` and gives the
 * sidecar boot a generous timeout (uvicorn cold-start + dummy
 * judge/embedding warm-up can take several seconds in CI).
 */
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/e2e/**/*.test.ts"],
    exclude: ["node_modules", "dist"],
    // Sidecar boot in CI can take ~10s; per-test timeout is generous
    // because the firewall pipeline runs the dummy judge for every
    // call (still <1s, but we'd rather not flake on slow runners).
    testTimeout: 30_000,
    hookTimeout: 60_000,
    // Single thread so beforeAll's sidecar subprocess is not
    // spawned per-worker.
    pool: "forks",
    poolOptions: { forks: { singleFork: true } },
  },
});
