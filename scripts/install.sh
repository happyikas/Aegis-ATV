#!/usr/bin/env bash
# Aegis Personal — one-line installer
#
#   curl -LsSf https://raw.githubusercontent.com/happyikas/Aegis-ATV/main/scripts/install.sh | bash
#
# What this does, in order:
#   1. Confirms platform (macOS arm64/x86_64 or Linux x86_64).
#   2. Installs `uv` if it's missing.
#   3. Clones (or fast-pulls) Aegis-ATV into ${AEGIS_HOME:-~/.aegis-src}.
#   4. Runs `uv sync`.
#   5. Runs `uv run aegis install --mode local` to patch
#      ~/.claude/settings.json with the PreToolUse hook.
#
# What this does NOT do:
#   * Make any cloud calls (Solo Free contract).
#   * Modify any file outside $AEGIS_HOME and ~/.claude/.
#   * Run as a privileged user — refuses if invoked that way.
#
# Re-running is idempotent: clone -> pull, sync no-ops if up-to-date,
# install creates a timestamped backup of ~/.claude/settings.json.

set -euo pipefail

AEGIS_REPO="${AEGIS_REPO:-https://github.com/happyikas/Aegis-ATV.git}"
AEGIS_HOME="${AEGIS_HOME:-${HOME}/.aegis-src}"
AEGIS_REF="${AEGIS_REF:-main}"

c_red()    { printf '\033[31m%s\033[0m' "$*"; }
c_green()  { printf '\033[32m%s\033[0m' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m' "$*"; }
c_blue()   { printf '\033[34m%s\033[0m' "$*"; }
c_dim()    { printf '\033[2m%s\033[0m' "$*"; }

step() { printf "%s %s\n" "$(c_blue '==>')" "$*"; }
ok()   { printf "%s %s\n" "$(c_green '✓')" "$*"; }
warn() { printf "%s %s\n" "$(c_yellow '!')" "$*" >&2; }
die()  { printf "%s %s\n" "$(c_red '✗')" "$*" >&2; exit 1; }

# ── pre-flight ───────────────────────────────────────────────────────

if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
  die "Refusing to run as the privileged 'root' user. Aegis Personal is a per-user install — re-run as your normal account."
fi

case "$(uname -s)" in
  Darwin) os="macos" ;;
  Linux)  os="linux" ;;
  *) die "Unsupported OS: $(uname -s). Aegis Personal supports macOS and Linux. Open an issue if you need Windows/WSL2 guidance." ;;
esac

case "$(uname -m)" in
  arm64|aarch64) arch="arm64" ;;
  x86_64|amd64)  arch="x86_64" ;;
  *) die "Unsupported arch: $(uname -m)" ;;
esac

step "Detected platform: $(c_green "$os/$arch")"

# ── 1. uv ────────────────────────────────────────────────────────────

if ! command -v uv >/dev/null 2>&1; then
  step "Installing uv (Python toolchain)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin or ~/.cargo/bin; make it visible for this session
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
  command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH. Add ~/.local/bin to your shell profile and re-run."
  ok "uv installed: $(uv --version)"
else
  ok "uv already present: $(uv --version)"
fi

# ── 2. clone / update ────────────────────────────────────────────────

if ! command -v git >/dev/null 2>&1; then
  die "git not found. Install git first (macOS: 'xcode-select --install', Debian/Ubuntu: 'apt install git')."
fi

if [[ -d "${AEGIS_HOME}/.git" ]]; then
  step "Updating existing checkout at $(c_dim "${AEGIS_HOME}")..."
  git -C "${AEGIS_HOME}" fetch --quiet origin "${AEGIS_REF}"
  git -C "${AEGIS_HOME}" checkout --quiet "${AEGIS_REF}"
  git -C "${AEGIS_HOME}" pull --ff-only --quiet origin "${AEGIS_REF}"
  ok "Checkout up to date: $(git -C "${AEGIS_HOME}" log -1 --format='%h %s')"
else
  step "Cloning into $(c_dim "${AEGIS_HOME}")..."
  mkdir -p "$(dirname "${AEGIS_HOME}")"
  git clone --quiet --branch "${AEGIS_REF}" --depth 1 "${AEGIS_REPO}" "${AEGIS_HOME}"
  ok "Cloned $(c_dim "${AEGIS_REF}") into ${AEGIS_HOME}"
fi

# ── 3. dependencies ──────────────────────────────────────────────────

step "Installing Python dependencies (uv sync)..."
(cd "${AEGIS_HOME}" && uv sync --quiet)
ok "Dependencies ready"

# ── 4. install hook ──────────────────────────────────────────────────

step "Patching ~/.claude/settings.json with Aegis PreToolUse hook..."
(cd "${AEGIS_HOME}" && uv run aegis install --mode local)

# ── 5. summary ───────────────────────────────────────────────────────

cat <<EOF

$(c_green '─── Aegis Personal installed ───')

Source checkout    : ${AEGIS_HOME}
Hook configuration : ${HOME}/.claude/settings.json (backup created automatically)
Audit log          : ${HOME}/.aegis/audit.jsonl

$(c_yellow 'Next steps:')

  1. Fully quit and relaunch Claude Code.
     (Reopening a tab is not enough — the settings.json change
     is read at process startup.)

  2. In your next Claude Code session, ask it to do something
     destructive. Aegis will BLOCK it before the tool runs and
     append a signed line to ${HOME}/.aegis/audit.jsonl.

  3. Inspect what was caught:

       cd ${AEGIS_HOME}
       uv run aegis report          # 5-line risk summary
       uv run aegis verify-audit    # cryptographic chain check

To uninstall:

       cd ${AEGIS_HOME}
       uv run aegis uninstall

Documentation:
  ${AEGIS_HOME}/docs/PERSONAL_QUICKSTART.md
  https://github.com/happyikas/Aegis-ATV

EOF
