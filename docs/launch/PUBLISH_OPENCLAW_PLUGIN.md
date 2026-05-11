# `@happyikas/openclaw-plugin-aegis` npm publish runbook

Step-by-step commands for publishing the OpenClaw plugin to npm.
Mirrors the structure of [`RELEASE_v0.2.0.md`](RELEASE_v0.2.0.md)
(the Aegis Python core's release runbook), scoped to the
TypeScript plugin which versions independently.

> **Why a separate runbook**: the plugin is a separate npm package
> with its own versioning, its own CI workflow, and a different
> release flow than `aegis-mvp` (no GPG signed tags, no Homebrew
> Formula). This is its own contract.

---

## 0.3.0 GA — 2026-05-11 (published)

The `-preview` suffix lifted in PR #164. The diff against
`0.2.0-preview.2` was metadata-only (version bumps, CHANGELOG,
README "Status" wording, removal of the `@preview` install
caveat); the runtime behavior is unchanged. E2E CI ran green
through the soak window (PR #143 shipped the infra on 2026-05-09)
with zero flake before the publish call. Post-publish housekeeping:

```bash
npm publish                                                          # 0.3.0 → `latest`
npm dist-tag rm @happyikas/openclaw-plugin-aegis preview             # retire preview channel
npm deprecate @happyikas/openclaw-plugin-aegis@0.2.0-preview.2 ""    # lift deprecation msg
```

The remainder of this runbook documents the **0.2.0-preview.X**
publish flow that originally landed the package on npm. Keep for
historical reference; the GA publish above is the canonical
go-forward path.

---

## 0. Pre-flight (one-time)

### 0.1. npm account + access to `@openclaw` org

```bash
# If you don't already have one, create an npm account:
#   https://www.npmjs.com/signup
# Then make sure you're a member (or admin) of the @openclaw org.

# Verify CLI auth:
npm whoami
# → your-username

# If you're not logged in:
npm login
# (browser flow → npm token saved to ~/.npmrc)
```

### 0.2. Two-factor authentication

`@openclaw` is a scoped public package. npm enforces 2FA for any
publish under a scoped org. Make sure you have a TOTP authenticator
configured:

```bash
npm profile get
# → "tfa": "auth-and-writes" (REQUIRED for org packages)
```

If 2FA is `auth-only`, upgrade to `auth-and-writes`:

```bash
npm profile enable-2fa auth-and-writes
```

You'll be prompted for a TOTP token on every `npm publish`.

### 0.3. (Optional) Provenance / SLSA attestation

npm supports build-provenance via GitHub Actions. We're publishing
manually for `0.2.0-preview.1` to validate the metadata first; once
the package is on the registry we can switch to GH Actions
publishing with provenance for `0.3.0+`. See `release.yml` follow-up.

---

## 1. Bump version + commit (already done in this PR)

If you're reading this *after* the prep PR merged, the `package.json`
version is already at `0.2.0-preview.1`. Skip to step 2.

If you're prepping a future release:

```bash
cd openclaw-plugin
npm version 0.3.0-preview.1  # or semver-bump command of your choice
# → updates package.json + creates a git tag (delete the tag if you
#   want the v0.2.0 main-aegis tag to remain authoritative)
git tag -d v0.3.0-preview.1   # if `npm version` created one
```

Add a CHANGELOG.md entry, commit, open PR, merge.

---

## 2. Build + test + dry-run pack

```bash
cd openclaw-plugin

# Clean rebuild from current source:
rm -rf dist/
npm install
npm run build      # tsc → dist/
npm test           # vitest, all green

# See exactly what would be published (no actual upload):
npm pack --dry-run
# This prints the file list and the resulting tarball size.
# Verify:
#   - dist/index.js + dist/index.d.ts present
#   - README.md, CHANGELOG.md, openclaw.plugin.json, LICENSE present
#   - tests/, src/, node_modules/, .git/ ABSENT
```

The expected file count is roughly **15-20 files**, tarball size
**~10-30 KB**. If the file list looks bloated (e.g., includes
node_modules or test fixtures), revisit the `files` field in
`package.json`.

---

## 3. Publish to npm

> ⚠️ **Pre-release versions require `--tag`** — versions with a
> hyphen suffix (`-preview.N`, `-rc.N`, `-beta.N`) MUST be published
> with `npm publish --tag <name>`. Without `--tag`, npm rejects the
> publish (since v9). Even if the publish succeeded, omitting the
> tag would set the pre-release as the default `latest` install
> for anyone running `npm install @happyikas/openclaw-plugin-aegis`, which
> is exactly what we don't want for a preview release.

```bash
# Pre-release: tag as `preview` so users opt in explicitly.
# Will prompt for 2FA TOTP.
npm publish --tag preview

# After the GA release lifts the -preview suffix, the same package
# publishes with the default tag (`npm publish` is sufficient).
```

`--dry-run` first to verify metadata + see what files would ship:

```bash
npm publish --dry-run --tag preview
```

Successful publish prints:

```
+ @happyikas/openclaw-plugin-aegis@0.2.0-preview.2
```

After publish, users install with the explicit tag:

```bash
# Pre-release install (this is what your README documents):
npm install @happyikas/openclaw-plugin-aegis@preview
# (resolves to the highest version tagged `preview`)

# Pin to the exact pre-release:
npm install @happyikas/openclaw-plugin-aegis@0.2.0-preview.2

# Default install (after GA):
npm install @happyikas/openclaw-plugin-aegis
```

---

## 4. Smoke test (verify install on a clean env)

```bash
# Make a throwaway dir
mkdir -p /tmp/aegis-plugin-smoke && cd /tmp/aegis-plugin-smoke
npm init -y
npm install @happyikas/openclaw-plugin-aegis@0.2.0-preview.1

# Verify the entry surface:
node --input-type=module -e "
import('@happyikas/openclaw-plugin-aegis').then(m => {
  console.log('exports:', Object.keys(m).sort());
  console.log('default config:', m.DEFAULT_CONFIG);
});
"
# → exports: [ 'AegisSidecarError', 'DEFAULT_CONFIG', 'activate', 'default', 'evaluate', 'handleBeforeToolCall' ]
# → default config: { aegisUrl: 'http://localhost:8000', tenantId: 'default', timeoutMs: 1500, failClosed: false }
```

If the smoke test passes you're done.

---

## 5. Post-publish checklist

- [ ] Confirm npm registry shows the version: <https://www.npmjs.com/package/@happyikas/openclaw-plugin-aegis>
- [ ] (Optional) Tweet / post the release link.
- [ ] Update [`docs/releases/OPENCLAW_LOCAL.ko.md`](../releases/OPENCLAW_LOCAL.ko.md) and [`docs/releases/OPENCLAW_CLOUD.ko.md`](../releases/OPENCLAW_CLOUD.ko.md) roadmap tables to mark step 8 ✅ in a follow-up commit.
- [ ] Open a tracking issue for the next release's E2E test against a running OpenClaw runtime.

---

## Rollback

npm scoped packages allow `npm unpublish` within **72 hours** of
publishing, AS LONG AS no other public packages depend on them. Use
this only if you discover a serious metadata or contents issue
right after publishing:

```bash
npm unpublish @happyikas/openclaw-plugin-aegis@0.2.0-preview.1
```

After 72 hours, or if any downstream package depends on it,
`unpublish` is rejected. The remediation is to publish a patch
release with the fix (e.g., `0.2.0-preview.2`) and document the
issue in the CHANGELOG.

For a deprecated-but-not-removed flow:

```bash
npm deprecate @happyikas/openclaw-plugin-aegis@0.2.0-preview.1 \
  "Superseded by 0.2.0-preview.2; see CHANGELOG"
```

This keeps the artifact but warns anyone installing it.

---

## Why we publish a `-preview` suffix

Per `CHANGELOG.md`, the plugin doesn't yet have an end-to-end
integration test against a running OpenClaw runtime. The handler is
mock-tested with `vi.fn()` fetch — sufficient to validate the
verdict mapping logic, but not sufficient to claim production
readiness. The `-preview.N` suffix communicates this honestly to
npm consumers.

The next release lifts the suffix once we have a live integration
test confirming end-to-end behaviour against a real OpenClaw +
Aegis sidecar pair.
