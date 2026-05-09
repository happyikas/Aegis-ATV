# `@happyikas/openclaw-plugin-aegis` Changelog

This package versions independently of the `aegis-mvp` Python core
(see `pyproject.toml` for the core's version). The plugin's
`apiVersion` field in `openclaw.plugin.json` tracks OpenClaw's
plugin SDK contract; this CHANGELOG tracks the plugin's own
release line.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/),
with the **`-preview.N`** suffix marking releases that lack an
end-to-end integration test against a running OpenClaw runtime.

## [Unreleased]

### Added

* **End-to-end integration test** (`tests/e2e/sidecar.e2e.test.ts`) —
  boots the real Aegis Python sidecar in a subprocess and runs the
  plugin's `handleBeforeToolCall` against it over HTTP. Covers ALLOW,
  REQUIRE_APPROVAL (sensitive-path Read), BLOCK (cloud_destructive),
  multi-channel attribution round-trip, multi-provider attribution
  round-trip, and audit-chain anchor (atv_id + Ed25519 signature
  present in the verdict body). Lifts the gate that earlier CHANGELOG
  entries described as "no end-to-end test against a running OpenClaw
  runtime + Aegis sidecar has been performed" — the OpenClaw runtime
  side is still simulated (we don't `npm install openclaw` in CI),
  but the plugin → sidecar half is now proven against the real Python
  firewall pipeline.
* **`npm run test:e2e`** script + dedicated `vitest.e2e.config.ts`
  with longer hook/test timeouts for sidecar boot.
* **CI `e2e` job** in `.github/workflows/openclaw-plugin.yml` —
  installs uv + Python 3.11 + Aegis sidecar deps, then runs the
  vitest E2E suite. Triggers on changes to `src/aegis/api/evaluate.py`
  in addition to `openclaw-plugin/**` so a sidecar-side schema break
  is caught before merge.

The next release that lifts the `-preview` suffix will land once
this test runs green on CI for ≥ 7 days without flake.

## [0.2.0-preview.2] — 2026-05-09 — published

**Live on npm**:
<https://www.npmjs.com/package/@happyikas/openclaw-plugin-aegis>

```bash
npm install @happyikas/openclaw-plugin-aegis@preview
```

> **npm scope** — published as `@happyikas/openclaw-plugin-aegis`
> on npm. Earlier git history shows the planned name as
> `@openclaw/plugin-aegis`, but the `@openclaw` npm org isn't
> registered (and may belong to OpenClaw upstream when they
> register it), so the first actual publish ships under the
> maintainer's own `@happyikas` scope. If/when OpenClaw publishes
> their own `@openclaw` npm org, the package republishes there
> with a redirect note in this CHANGELOG.

> **Deprecation note on `latest`** — npm's first-publish behaviour
> auto-applied the `latest` dist-tag to `0.2.0-preview.2` alongside
> the intended `preview` tag (npm requires every published package
> to have a `latest`, and there's only one version yet). To prevent
> users from silently installing the prerelease via the unsuffixed
> `npm install @happyikas/openclaw-plugin-aegis`, the version is
> annotated via `npm deprecate` with: *"Preview release. Use
> @preview tag explicitly."* The deprecation will lift on the
> first GA release (e.g. `0.3.0`) which will become the new
> `latest`.

Pre-publish completeness review (`PUBLISH_OPENCLAW_PLUGIN.md`)
identified three blocker-class issues in the `0.2.0-preview.1`
release-prep PR. This patch release fixes them before the package
ever hits npm — `0.2.0-preview.1` is therefore *also* never
published, only present in repo git history.

### Fixed

* **`openclaw.plugin.json` version drift** — manifest still said
  `0.1.0-preview.1` while `package.json` was at `0.2.0-preview.1`.
  Now both at `0.2.0-preview.2` and stay in sync.
* **`timeoutMs` description was wrong** — said "set to 0 to fail-
  closed instead", but the actual fail-closed control is the
  separate `failClosed` boolean. Setting `timeoutMs: 0` would
  silently fail-OPEN (the opposite of what the doc claimed). The
  manifest description now accurately separates the timeout value
  from the post-error policy.
* **`prepublishOnly` only ran the build, not the tests** — could
  publish a tarball with a green build but red tests. Now runs
  `npm run lint && npm test && npm run build`.

### Added

* **Sidecar version-mismatch hint** — when the Aegis sidecar
  returns 404 on `/evaluate/openclaw`, `AegisSidecarError` now
  surfaces "sidecar may be too old; @openclaw/plugin-aegis >= 0.2.0
  requires aegis-mvp >= 0.2.0". Replaces silent fail-open against
  v0.1.0 sidecars (the wrong default for a security plugin).
* **`activate()` entry-point tests** (12 new vitest cases) —
  registration with the right event name, optional-chained
  `api.config?.()`, DEFAULT_CONFIG / user-config merge, default
  export shape, public re-export surface. Existing 19 tests for
  the verdict mapping kept green.

### Documentation

* **README usage section expanded** — concrete first-30-seconds
  example showing where the plugin entry goes in an OpenClaw
  project, what `openclaw.plugin.json` looks like in user code,
  config override syntax, expected logs.

## [0.2.0-preview.1] — 2026-05-09 (never published)

Initially the planned npm release; superseded by `0.2.0-preview.2`
during pre-publish review (see entry above). All `0.2.0-preview.1`
features carried forward.

### Added

* **`POST /evaluate/openclaw`** route support — the plugin now POSTs
  to the dedicated adapter endpoint instead of the legacy `/evaluate`
  (which expects the full ATVInput shape). Closes the schema
  mismatch that was present in `0.1.0-preview.1`. Requires Aegis
  sidecar **v0.2.0+** (`pip install aegis-mvp>=0.2.0`).
* **Multi-channel attribution** — the `channel` field on
  `OpenClawBeforeToolCallEvent` now flows through to the Aegis audit
  record. Operators can group reports by channel
  (`aegis report --by-channel`).
* **Multi-provider attribution** — same as above for `provider`.
  Combined with Aegis v0.2.0's `aegis report --by-provider` and the
  provider-divergence advisor, this is the foundation for
  cross-provider safety drift detection.

### Documentation

* README now includes a verdict-mapping table + architecture diagram +
  honest-limitations section (no E2E test yet, no npm publish was
  attempted in 0.1.0-preview.1).

### Honest scope

* Still preview. The handler is mock-tested with `vi.fn()` fetch — no
  end-to-end test against a running OpenClaw runtime + Aegis sidecar
  has been performed. The next release (`0.3.0` or `1.0.0` depending
  on E2E results) will lift the preview suffix.

## [0.1.0-preview.1] — 2026-05-08

Initial skeleton (never published to npm).

### Added

* Initial TypeScript skeleton: `activate()` entry, `before_tool_call`
  handler, `aegis-client.ts` HTTP wrapper, configurable fail-open vs
  fail-closed behaviour.
* Verdict mapping for ALLOW (with optional `params` rewrite),
  REQUIRE_APPROVAL (with severity + timeout), BLOCK.
* 19 vitest tests covering verdict mapping, request shape, sidecar
  error paths.
* Separate GitHub Actions workflow (`.github/workflows/openclaw-plugin.yml`).

### Note

This version was built but never published to npm (the public release
process was deferred to `0.2.0-preview.1`). If you encounter a
`0.1.0-preview.1` reference in the repo's git history, that's the
skeleton — the published artifact starts at `0.2.0-preview.1`.
