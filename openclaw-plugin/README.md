# @happyikas/openclaw-plugin-aegis

[![npm](https://img.shields.io/npm/v/@happyikas/openclaw-plugin-aegis.svg)](https://www.npmjs.com/package/@happyikas/openclaw-plugin-aegis)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](./LICENSE)

> **Note on the npm scope** — published under `@happyikas` (the
> Aegis ATV maintainer's npm scope) until OpenClaw publishes an
> official `@openclaw` npm org. If/when that happens, the package
> will republish to `@openclaw/plugin-aegis` with a redirect note
> here. For now, all install commands use `@happyikas/openclaw-
> plugin-aegis`.

OpenClaw plugin that runs every tool call through [Aegis ATV](https://github.com/happyikas/Aegis-ATV)'s 16-step firewall + cryptographic audit chain. Maps Aegis verdicts (`ALLOW` / `REQUIRE_APPROVAL` / `BLOCK`) to OpenClaw's `before_tool_call` return contract, plus param-rewrite for automatic redaction.

> **Status**: Preview (`0.2.0-preview.2`). TypeScript handler + Aegis HTTP client + multi-channel + multi-provider attribution are implemented and unit-tested with mocked responses (31 vitest cases). End-to-end integration with a running OpenClaw runtime + Aegis sidecar lifts the preview suffix in the next release.

## Compatibility

| Plugin version | Aegis sidecar | Notes |
|----------------|---------------|-------|
| `0.2.0-preview.2` (this) | `aegis-mvp >= 0.2.0` | POSTs to `/evaluate/openclaw` route; multi-channel + multi-provider attribution; pre-publish blocker fixes (manifest version sync, manifest doc accuracy, `prepublishOnly` runs tests, sidecar version-mismatch hint, 12 entry-point tests) |

The plugin's `apiVersion: 1` field in `openclaw.plugin.json` tracks the OpenClaw plugin SDK contract — independent of this package version.

## Install

This package is currently published under the `preview` npm tag — `npm install @happyikas/openclaw-plugin-aegis` will not resolve under the default `latest` tag until the first GA release. Use the tag explicitly:

```bash
# Latest preview:
npm install @happyikas/openclaw-plugin-aegis@preview

# Pin to an exact pre-release version:
npm install @happyikas/openclaw-plugin-aegis@0.2.0-preview.2
```

You also need the Aegis sidecar service running at `http://localhost:8000` (default). To start it:

```bash
pip install aegis-mvp>=0.2.0
docker compose -f $(python -c "import aegis;import pathlib;print(pathlib.Path(aegis.__file__).parent.parent.parent/'docker-compose.yml')") up -d

# OR clone the repo if you want to run from source:
git clone https://github.com/happyikas/Aegis-ATV.git && cd Aegis-ATV
docker compose up -d
```

## Usage — first 30 seconds

A typical OpenClaw plugin lives at `<your-project>/plugins/aegis/` with at least three files:

```
my-openclaw-bot/
├── plugins/
│   └── aegis/
│       ├── index.ts                  ← entry point (calls activate)
│       ├── openclaw.plugin.json      ← per-install config
│       └── package.json              ← declares dependency on this package
└── ...
```

**`plugins/aegis/index.ts`** — the entry point OpenClaw loads:

```ts
import { activate } from "@happyikas/openclaw-plugin-aegis";
import type { OpenClawPluginApi } from "@happyikas/openclaw-plugin-aegis";

// OpenClaw calls this default export when activating the plugin.
export default function (api: OpenClawPluginApi) {
  activate(api);
}
```

**`plugins/aegis/openclaw.plugin.json`** — overrides for this install (all fields optional, defaults shown):

```json
{
  "apiVersion": 1,
  "name": "aegis",
  "configuration": {
    "aegisUrl":   "http://localhost:8000",
    "tenantId":   "my-bot-prod",
    "timeoutMs":  1500,
    "failClosed": false
  }
}
```

**`plugins/aegis/package.json`** — pins the plugin version:

```json
{
  "name": "my-openclaw-bot-aegis-plugin",
  "private": true,
  "type": "module",
  "dependencies": {
    "@happyikas/openclaw-plugin-aegis": "^0.2.0-preview"
  }
}
```

That's it. Every `before_tool_call` event in your OpenClaw bot now flows through Aegis.

### What happens at runtime

For a tool call OpenClaw is about to execute:

1. OpenClaw fires `before_tool_call` with `{ tool, params, channel?, provider?, sessionId? }`.
2. Plugin POSTs an `OpenClawEvaluateRequest` to `<aegisUrl>/evaluate/openclaw`.
3. Aegis sidecar runs the 16-step firewall + signs the audit record.
4. Sidecar returns `{ decision: "ALLOW" | "REQUIRE_APPROVAL" | "BLOCK", reason, trace_id, ... }`.
5. Plugin maps the decision to OpenClaw's return contract:
   - `ALLOW` (no rewrite) → `undefined` (continue)
   - `ALLOW` + `sanitized_input` → `{ params: <sanitized> }` (auto-redaction)
   - `REQUIRE_APPROVAL` → `{ requireApproval: { title, description, severity, timeoutMs, timeoutBehavior } }`
   - `BLOCK` → `{ block: true, blockReason }`

### Logs you'll see

* On a clean ALLOW: nothing (plugin is silent in the happy path).
* On a sidecar error with `failClosed: false` (default): one stderr line, e.g.
  ```
  [aegis] sidecar error: Aegis /evaluate timed out after 1500ms — continuing (fail-open)
  ```
* On a sidecar error with `failClosed: true`: same warning + tool call BLOCKed.
* On a 404 (sidecar too old): the error message includes a hint to upgrade.

### Configuration reference

| Key | Default | Purpose |
|-----|---------|---------|
| `aegisUrl` | `http://localhost:8000` | Aegis sidecar URL. Use a per-tenant URL when running multiple isolated sidecars. |
| `tenantId` | `"default"` | Stamped on every audit record (use distinct tenants per channel/team for cleaner `aegis report --by-aid`). |
| `timeoutMs` | `1500` | AbortController timeout. Independent of fail-open vs fail-closed (which is set by `failClosed`). |
| `failClosed` | `false` | When `true`, sidecar errors return BLOCK instead of fail-open ALLOW. Recommended for regulated industries. |

## Verdict mapping

| Aegis decision | OpenClaw return |
|----------------|-----------------|
| `ALLOW` (no rewrite) | `undefined` (continue) |
| `ALLOW` with `sanitized_input` | `{ params: <sanitized> }` (auto-redaction) |
| `REQUIRE_APPROVAL` | `{ requireApproval: { title, description, severity, ... } }` |
| `BLOCK` | `{ block: true, blockReason }` |

## Development

```bash
npm install
npm test            # vitest
npm run build       # tsc → dist/
npm run lint        # tsc --noEmit
```

## Architecture

```
OpenClaw event (before_tool_call)
       │
       ▼
@openclaw/plugin-aegis (this package)
       │  POST /evaluate
       ▼
Aegis sidecar (Python, http://localhost:8000)
       │
       ├── 16-step firewall (step305 → step340)
       ├── ATV-2080-v1 vector evaluation
       ├── sLLM judge (free/pro/cloud profile)
       └── SHA3 + Ed25519 audit chain
       │
       ▼
verdict (ALLOW/REQUIRE_APPROVAL/BLOCK)
       │
       ▼
OpenClaw return contract (block/requireApproval/params)
```

## Honest limitations

- **No end-to-end test against real OpenClaw runtime yet** — handler is mock-tested with `vi.fn()` fetch.
- **First npm publish in progress** — published as `0.2.0-preview.2` (the `-preview.N` suffix marks "no E2E test against a running OpenClaw runtime yet"; lifts in the next minor release).
- **Schema sync** — Aegis Python `EvaluateRequest` / `EvaluateResponse` are mirrored in `src/types.ts` by hand; future work: codegen from Pydantic models.
- **No streaming verdicts** — single-shot per tool call. Streaming is a future Aegis API addition.

## Roadmap

| Item | Status |
|------|--------|
| Initial skeleton + handler + 19 vitest tests | ✅ shipped in `0.1.0-preview.1` (skeleton, never published) |
| `/evaluate/openclaw` adapter route | ✅ shipped in this release |
| Multi-channel attribution (`channel` field) | ✅ shipped in this release |
| Multi-provider attribution (`provider` field) | ✅ shipped in this release |
| End-to-end test against `docker compose up` Aegis sidecar | 🟡 next release (lifts the preview suffix) |
| Codegen TypeScript types from Aegis Pydantic schema | 🟡 future |
| Streaming verdicts (mid-tool-call cancellation) | 🔴 future Aegis API addition |
| ClawHub marketplace listing | 🔴 follow-up |

## What's new in `0.2.0-preview.1`

* The plugin now POSTs to `/evaluate/openclaw` (the new adapter route added in `aegis-mvp 0.2.0`) instead of the legacy `/evaluate` (which expects the full ATVInput shape). This closes the schema mismatch that was present in the never-published `0.1.0-preview.1` skeleton.
* Multi-channel + multi-provider attribution flow into the Aegis audit record. Combined with `aegis report --by-channel` and `aegis report --by-provider`, this is the foundation for cross-channel and cross-provider safety drift detection.

See [CHANGELOG.md](./CHANGELOG.md) for the full version history.

## License

Apache-2.0 — see [LICENSE](../LICENSE).
