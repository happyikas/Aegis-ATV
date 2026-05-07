# Homebrew distribution — Aegis Personal

This directory contains release / maintenance docs for the Homebrew
distribution. The formula itself lives at
[`Formula/aegis.rb`](../../Formula/aegis.rb) (Homebrew's canonical
tap layout — formulas under `Formula/` at the repo root).

## For users — installing via Homebrew

```bash
brew tap happyikas/aegis https://github.com/happyikas/Aegis-ATV.git
brew install happyikas/aegis/aegis
aegis install --mode local
# Restart Claude Code.
```

That's it. `brew upgrade aegis` will pull future versions.

If you'd rather avoid Homebrew entirely, use the one-line installer:

```bash
curl -LsSf https://raw.githubusercontent.com/happyikas/Aegis-ATV/main/scripts/install.sh | bash
```

The installer is functionally equivalent — it pins `uv`, clones into
`~/.aegis-src`, and runs `aegis install --mode local` for you.

## For maintainers — cutting a Homebrew release

When tagging a new public release:

```bash
# 1. Tag the release in the repo
git tag -a vX.Y.Z -m "..."
git push origin vX.Y.Z
gh release create vX.Y.Z --generate-notes

# 2. Compute the tarball SHA-256
SHA=$(curl -sL https://github.com/happyikas/Aegis-ATV/archive/refs/tags/vX.Y.Z.tar.gz \
        | shasum -a 256 | awk '{print $1}')
echo "$SHA"

# 3. Update Formula/aegis.rb in this repo
#    - Change `url "...refs/tags/vX.Y.Z.tar.gz"`
#    - Change `sha256 "..."` to the value from step 2
sed -i '' \
  -e "s|refs/tags/v[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+\\.tar\\.gz|refs/tags/vX.Y.Z.tar.gz|" \
  -e "s|sha256 \"[0-9a-f]\\{64\\}\"|sha256 \"$SHA\"|" \
  Formula/aegis.rb

# 4. Commit + open PR
git checkout -b release/vX.Y.Z-brew
git commit -am "release: brew formula -> vX.Y.Z"
gh pr create --fill

# 5. After the PR merges, users running `brew update && brew upgrade aegis`
#    will pick up the new formula automatically (no separate
#    homebrew-aegis tap repository needed — the tap URL points at this
#    repo and brew finds the formula under Formula/ or any *.rb file).
```

## Homebrew-core graduation (deferred)

The current setup uses a **third-party tap** rather than the official
`homebrew-core` repository. To submit to homebrew-core later we'll
need:

* A stable LICENSE file in the project root (currently TBD).
* At least 30 days of public release history with no breaking changes.
* A test block that does not require network access (already true).
* No optional Python dependencies that fetch model files at install
  time (the `local-llm` extra is opt-in and not installed by the
  formula's `uv sync --no-dev`).

Once those are met, `brew bump-formula-pr` against
`Homebrew/homebrew-core` is the standard path.

## Why a self-hosted tap and not a separate `homebrew-aegis` repo?

Homebrew supports tap repositories under any URL since 2021 — pointing
the tap directly at this repo means:

* No second repo to keep in sync with releases.
* CI on the formula runs against the same commit as the source.
* Users see exactly one "source of truth" for the project.

Trade-off: formula bumps are commits to this repo, which slightly
inflates the changelog. We accept this for the simpler single-repo
mental model.
