# v0.2.0 release runbook

After PR [#130](https://github.com/happyikas/Aegis-ATV/pull/130)
merges, run the steps below to actually publish v0.2.0. Each step
is idempotent (rerun-safe) and the runbook stops at the first
failure.

> **Why a separate runbook**: GPG / SSH signing requires the user's
> private key, which can't (and shouldn't) be in the repo or in
> CI. PyPI publish requires the user's API token. So these steps
> run on the maintainer's local machine, not in GitHub Actions.

---

## 0. Pre-flight (one-time)

### 0.1. Set up signed-tag signing — pick **A or B**, not both

#### A) **SSH-key signing** (simplest — recommended if you already
have an SSH key on GitHub)

```bash
# Use your existing GitHub SSH key — already trusted, "Verified"
# badge shows up on GitHub same as with GPG.
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global tag.gpgsign true

# Tell GitHub which SSH key to trust for signing (one-time):
#   Settings → SSH and GPG keys → New SSH key
#   Key type: Signing Key
#   (paste contents of ~/.ssh/id_ed25519.pub)
```

#### B) **GPG signing** (if you specifically want GPG)

```bash
# Generate a new key (skip if you have one):
gpg --full-generate-key
# - Type: 1 (RSA and RSA), 4096 bits, no expiration, your name+email

# Find the key ID:
gpg --list-secret-keys --keyid-format=long
#   sec   rsa4096/ABCD1234EF567890 ...   ← that's your key ID

# Configure git:
git config --global user.signingkey ABCD1234EF567890
git config --global tag.gpgsign true

# Export public key + add to GitHub:
gpg --armor --export ABCD1234EF567890
#   Settings → SSH and GPG keys → New GPG key → paste

# (Optional) export to a backup file:
gpg --export-secret-keys --armor ABCD1234EF567890 > ~/aegis-gpg-backup.asc
```

### 0.2. PyPI API token

```bash
# Visit https://pypi.org/manage/account/token/
# Create a project-scoped token: "aegis-mvp" project, scope-token-only.
# Save in shell config (NOT in repo):
export UV_PUBLISH_TOKEN="pypi-...your-token..."
# Optional: persist via 1Password / direnv / shell profile.
```

> Until v0.2.0 lands on PyPI, you can't scope the token to the
> project. Use a **user-scoped** token for the first publish, then
> rotate to a project-scoped one immediately after.

---

## 1. Tag + push

After PR #130 merges:

```bash
git switch main && git pull origin main

# Verify what we're about to tag:
git log -1 --oneline   # should be the v0.2.0 release-prep commit
cat pyproject.toml | grep '^version'   # 0.2.0

# Create signed tag with the v0.2.0 changelog block as the message:
TAG_BODY=$(awk '/^## \[0\.2\.0\]/{flag=1;next} /^## \[0\.1\.0\]/{flag=0} flag' CHANGELOG.md)
git tag -s v0.2.0 -m "Aegis ATV v0.2.0 — Coach / Live / Doctor + three release tracks" -m "$TAG_BODY"

# Verify the signature locally before pushing:
git tag --verify v0.2.0    # should print "Good signature from..."

# Push:
git push origin v0.2.0
```

---

## 2. GitHub release

```bash
# Extract the v0.2.0 changelog block as release notes:
awk '/^## \[0\.2\.0\]/{flag=1;next} /^## \[0\.1\.0\]/{flag=0} flag' CHANGELOG.md > /tmp/release-notes-v0.2.0.md

# Create the release:
gh release create v0.2.0 \
    --title "v0.2.0 — Coach / Live / Doctor + three release tracks" \
    --notes-file /tmp/release-notes-v0.2.0.md \
    --verify-tag

# Verify:
gh release view v0.2.0
```

---

## 3. PyPI publish

```bash
# Clean rebuild from the tagged commit:
rm -rf dist/
uv build

# Verify the version stamp on the artifact:
ls dist/   # should be aegis_mvp-0.2.0.tar.gz + .whl

# Dry run (optional — checks credentials + metadata without uploading):
uv publish --check-url https://test.pypi.org/legacy/ --dry-run dist/*

# Real publish (requires UV_PUBLISH_TOKEN env):
uv publish dist/*

# Verify:
pip index versions aegis-mvp
# or:
curl -s https://pypi.org/pypi/aegis-mvp/json | jq '.info.version'
```

---

## 4. Update Formula sha256 (follow-up PR)

The Formula's `url` in PR #130 already points at `v0.2.0`, but the
`sha256` is a placeholder of zeros. Now that the GitHub release
tarball exists, compute and patch:

```bash
# Compute the real sha256:
SHA=$(curl -sL https://github.com/happyikas/Aegis-ATV/archive/refs/tags/v0.2.0.tar.gz | shasum -a 256 | cut -d' ' -f1)
echo "$SHA"

# Open a follow-up PR:
git switch -c chore/formula-sha256-v0.2.0
sed -i.bak "s/0000000000000000000000000000000000000000000000000000000000000000/$SHA/" Formula/aegis.rb
rm Formula/aegis.rb.bak
git add Formula/aegis.rb
git commit -s -m "chore(formula): bump sha256 for v0.2.0 release tarball"
git push -u origin chore/formula-sha256-v0.2.0
gh pr create --base main --head chore/formula-sha256-v0.2.0 \
    --title "chore(formula): bump sha256 for v0.2.0 release tarball" \
    --body "Fills in the sha256 placeholder from PR #130 now that the v0.2.0 GitHub tarball exists. Computed: $SHA"
```

---

## 5. Smoke test (verify the chain end-to-end)

```bash
# 5.1. PyPI install on a clean venv:
python3 -m venv /tmp/aegis-smoke && /tmp/aegis-smoke/bin/pip install aegis-mvp==0.2.0
/tmp/aegis-smoke/bin/python -c "import aegis; print('PyPI version:', aegis.__version__)"
# → 0.2.0

# 5.2. Homebrew install (only after the Formula sha256 PR merges):
brew tap happyikas/aegis https://github.com/happyikas/Aegis-ATV.git
brew install --build-from-source happyikas/aegis/aegis
aegis --version

# 5.3. Source-install signed tag verification (any cloner can do
# this, not just the maintainer):
git clone https://github.com/happyikas/Aegis-ATV.git /tmp/aegis-clone
cd /tmp/aegis-clone
git verify-tag v0.2.0   # → "Good signature from..."
```

---

## 6. Announcement (optional but recommended)

After steps 1–5 pass:

1. Post the GitHub release URL to:
   - Hacker News (use [`docs/launch/SHOW_HN.md`](SHOW_HN.md) draft as the body)
   - r/ClaudeAI / r/LocalLLaMA (one post each)
   - LinkedIn / Twitter / personal blog
2. Update [`README.md`](../../README.md) badge versions if they're
   pinned.
3. Add a "what's next" comment on the GitHub release pointing at
   the v0.3.0 milestone (OpenClaw E2E + Ollama adapter).

---

## Rollback

If anything goes wrong **before step 3 (PyPI publish)** the rollback
is cheap:

```bash
git tag -d v0.2.0                  # local
git push --delete origin v0.2.0    # remote
gh release delete v0.2.0 --yes     # GitHub release
```

After step 3, **PyPI does not allow re-uploading the same version
number**. The recovery is to bump to v0.2.1 and re-run the runbook.
This is by design (PyPI's immutability guarantee). Plan for ≥ 1 hour
between "PR merge" and "PyPI publish" so that any last-minute issue
is caught before the irreversible step.

---

## Why this runbook isn't in CI

GitHub Actions could automate steps 1–4 with a `release.yml` workflow
triggered by tag push. We're deferring that until:

* The PyPI publish has been done manually at least once (so we know
  the metadata + classifiers + dependencies all resolve cleanly on
  the real index).
* The signed-tag policy is settled (SSH vs GPG; org-wide policy?).
* The Formula sha256 round-trip (currently a follow-up PR) is
  collapsed into the release workflow.

A v0.3.0 follow-up PR will introduce `release.yml` once those three
gates clear.
