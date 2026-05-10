# Release Pipeline — PyPI + GHCR

> One-time setup + per-release runbook. The pipeline ships
> `aegis-mvp` to **PyPI** and a multi-arch container image to
> **GitHub Container Registry (GHCR)** in lockstep, both triggered by
> pushing a `v*` git tag.

After the one-time setup in §1 + §2, every release is a 30-second
operation (§3).

---

## §1 One-time setup — PyPI trusted publisher

Trusted publishers eliminate long-lived API tokens. The PyPI side
authenticates against GitHub's OIDC token, scoped to this repo +
this workflow file.

1. **Create the project on PyPI** (one human, once):
   <https://pypi.org/manage/account/publishing/>

2. **Add a "trusted publisher" config** with:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `aegis-mvp` |
   | Owner | `happyikas` |
   | Repository | `Aegis-ATV` |
   | Workflow name | `release-pypi.yml` |
   | Environment name | `pypi` |

3. **Create the matching GitHub deployment environment** at
   <https://github.com/happyikas/Aegis-ATV/settings/environments>
   — name it `pypi`. No additional protection rules required, but
   **add a deployment branch rule** restricting to tag patterns
   `v*` so only tag pushes can deploy.

4. **(Recommended)** Configure 2FA on the PyPI account.

After step 2, the repo can publish with no API tokens — the action's
`id-token: write` permission generates a short-lived OIDC token that
PyPI verifies against the trusted-publisher config.

> See PyPI's docs for the canonical version of this setup:
> <https://docs.pypi.org/trusted-publishers/>

---

## §2 One-time setup — GHCR

GHCR uses the auto-issued `GITHUB_TOKEN` so there's no token
generation step. What you DO need:

1. **Confirm the package is public** after the first publish.
   Go to <https://github.com/users/happyikas/packages/container/aegis-atv/settings>
   (URL only valid after first publish) and set visibility to
   **Public**. Default is Private, which would force users to
   `docker login` before pulling.

2. **(Optional) Linking the package to the repo** — same settings
   page, "Manage Actions access" → add `happyikas/Aegis-ATV` with
   **Write** so the workflow can keep pushing without re-prompting.

The first publish will also have to be triggered manually
(workflow_dispatch with `dry-run=false`) so the package gets created
under the GHCR org. Subsequent tag pushes Just Work.

---

## §3 Per-release runbook

Once §1 + §2 are configured, a release is:

```bash
# 1. Bump version on a PR
sed -i '' 's/^version = "0.2.0"$/version = "0.3.0"/' pyproject.toml
# Update CHANGELOG.md with the new section
git checkout -b release/0.3.0
git commit -am "chore: bump version to 0.3.0"
git push -u origin release/0.3.0
gh pr create --title "chore: release 0.3.0" --body "Release notes: …"
# … wait for CI + merge …

# 2. After merge, sync main and tag the merge commit
git checkout main
git pull --ff-only origin main
git tag -s v0.3.0 -m "v0.3.0 — multi-agent + multi-LLM trio (Gaps A/B/C)"
git push origin v0.3.0

# 3. Watch the two workflows fire
gh run watch --workflow=release-pypi.yml
gh run watch --workflow=release-docker.yml
```

That's it. Both workflows guard on `tag matches pyproject.toml
version` so a misspelled tag fails fast instead of publishing the
wrong artifact.

The `:latest` Docker tag is **only** updated on stable tags (no `-`
in the version string). Prereleases like `v0.3.0-rc.1` get their
exact tag but don't touch `:latest`.

---

## §4 Dry-run before the first real release

Both workflows expose `workflow_dispatch` with a `dry-run` checkbox
(default `true`). This lets you verify the build artifacts without
actually pushing. Recommended dry-run sequence before the first real
tag:

```bash
# PyPI dry-run — builds wheel + sdist, uploads as workflow artifact,
# skips the publish step.
gh workflow run release-pypi.yml -f dry-run=true

# Docker dry-run — same idea; build runs but `push` is false.
gh workflow run release-docker.yml -f dry-run=true
```

Inspect the artifacts (PyPI: download from the run page; Docker:
check the build log for "would push: …"). When happy, proceed to §3.

---

## §5 Sdist / wheel size budget

`pip install aegis-mvp` should be fast. Current targets:

| Artifact | Target | As of v0.2.0 |
|---|---|---|
| sdist (`.tar.gz`) | < 1 MB | 628 KB |
| wheel (`.whl`)    | < 1 MB | 680 KB |

If a future release crosses 1 MB on either, add a budget-check step
to `release-pypi.yml` that fails the publish. The slim sdist is
enforced by `[tool.hatch.build.targets.sdist]` in `pyproject.toml`
(explicit `include` allow-list, which means a new top-level dir
won't accidentally bloat the sdist — it has to be added to the
allow-list explicitly).

---

## §6 Versioning

Semver. Backwards-incompatible firewall pipeline changes bump
**major**. New steps / new advisor signals bump **minor**. Bug fixes
+ new tests bump **patch**. Prereleases use `-rc.N` (release
candidate) or `-preview.N` (longer-running preview track).

The OpenClaw plugin (`@happyikas/openclaw-plugin-aegis` on npm)
versions independently — see `openclaw-plugin/CHANGELOG.md`.

---

## §7 Rollback

* **PyPI**: a published version cannot be deleted, only **yanked**.
  Yank with `pip install --upgrade twine && twine yank
  aegis-mvp==<bad version>`. Yanked versions still exist for
  reproducibility but new installs ignore them unless pinned
  explicitly.
* **GHCR**: a tag can be repointed by re-running the workflow
  against an earlier commit, OR the bad tag can be deleted from the
  package's GHCR settings page. Prefer "publish a fix forward" over
  "delete the bad tag" so users who already pulled the bad tag get
  a clear upgrade path.
