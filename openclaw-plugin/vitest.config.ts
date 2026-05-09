/**
 * Default vitest config — runs unit tests only.
 *
 * E2E tests live under `tests/e2e/**` and require the Aegis Python
 * sidecar to be bootable on the host (uv + Python 3.11+). They have
 * their own config (`vitest.e2e.config.ts`) and npm script
 * (`npm run test:e2e`) so the default `npm test` stays fast and
 * Python-free for plugin authors who only touch the TS surface.
 */
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    exclude: ["node_modules", "dist", "tests/e2e/**"],
  },
});
