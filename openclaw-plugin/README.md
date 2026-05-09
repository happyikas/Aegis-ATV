# @openclaw/plugin-aegis

[![npm](https://img.shields.io/npm/v/@openclaw/plugin-aegis.svg)](https://www.npmjs.com/package/@openclaw/plugin-aegis)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](../LICENSE)

OpenClaw plugin that runs every tool call through [Aegis ATV](https://github.com/happyikas/Aegis-ATV)'s 16-step firewall + cryptographic audit chain. Maps Aegis verdicts (`ALLOW` / `REQUIRE_APPROVAL` / `BLOCK`) to OpenClaw's `before_tool_call` return contract, plus param-rewrite for automatic redaction.

> **Status**: Preview (skeleton). The TypeScript handler + Aegis HTTP client are implemented and unit-tested with mocked responses. End-to-end integration with a running OpenClaw runtime + Aegis sidecar is the next milestone.

## Install

```bash
npm install @openclaw/plugin-aegis
```

You also need the Aegis sidecar service running at `http://localhost:8000` (default). To start it:

```bash
git clone https://github.com/happyikas/Aegis-ATV.git
cd Aegis-ATV
docker compose up -d
```

## Usage

```ts
import { activate } from "@openclaw/plugin-aegis";

// In your OpenClaw plugin entry point — OpenClaw passes the `api`:
export default function (api) {
  activate(api);
}
```

That's it. Every `before_tool_call` event now flows through Aegis.

### Configuration

Per-install overrides via `openclaw.plugin.json`:

| Key | Default | Purpose |
|-----|---------|---------|
| `aegisUrl` | `http://localhost:8000` | Aegis sidecar URL |
| `tenantId` | `"default"` | Stamped on every audit record (use distinct tenants per channel/team) |
| `timeoutMs` | `1500` | Sidecar evaluation timeout |
| `failClosed` | `false` | When true, sidecar errors BLOCK the call. Recommended for regulated industries. |

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
- **No npm publish** — package version is `0.1.0-preview.1`, not yet published.
- **Schema sync** — Aegis Python `EvaluateRequest` / `EvaluateResponse` are mirrored in `src/types.ts` by hand; future work: codegen from Pydantic models.
- **No streaming verdicts** — single-shot per tool call. Streaming is a future Aegis API addition.

## Roadmap (Issue tracker)

- [ ] End-to-end test against `docker compose up` Aegis sidecar
- [ ] Publish `@openclaw/plugin-aegis` to npm
- [ ] Codegen TypeScript types from Aegis Pydantic schema
- [ ] OpenClaw multi-channel attribution → ATV `header.channel` integration test
- [ ] Streaming verdicts (mid-tool-call cancellation)
- [ ] ClawHub marketplace listing

## License

Apache-2.0 — see [LICENSE](../LICENSE).
