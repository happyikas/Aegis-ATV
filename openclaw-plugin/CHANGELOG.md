# `@openclaw/plugin-aegis` Changelog

This package versions independently of the `aegis-mvp` Python core
(see `pyproject.toml` for the core's version). The plugin's
`apiVersion` field in `openclaw.plugin.json` tracks OpenClaw's
plugin SDK contract; this CHANGELOG tracks the plugin's own
release line.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/),
with the **`-preview.N`** suffix marking releases that lack an
end-to-end integration test against a running OpenClaw runtime.

## [0.2.0-preview.1] — 2026-05-09

First public preview release on npm.

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
