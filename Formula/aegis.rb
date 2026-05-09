#:  Aegis — Action Firewall for Claude Code (Personal MVP)
#:
#:  Tap-style formula. To use:
#:
#:    brew tap happyikas/aegis https://github.com/happyikas/Aegis-ATV.git
#:    brew install happyikas/aegis/aegis
#:
#:  After install:
#:
#:    aegis install --mode local      # patches ~/.claude/settings.json
#:    # restart Claude Code
#:
#:  This formula installs the `aegis` CLI plus the source tree under
#:  the cellar — the firewall is a per-tool-call hook that needs the
#:  full Python package available, not just an entrypoint script.

class Aegis < Formula
  desc "Action Firewall for Claude Code — 16-step ATV-2080-v1 pipeline, on-device, signed audit"
  homepage "https://github.com/happyikas/Aegis-ATV"
  url "https://github.com/happyikas/Aegis-ATV/archive/refs/tags/v0.2.0.tar.gz"
  sha256 "82f8ed49a6346777054f9222b8e082d5f3d2d85b1236895fbbf5bad2df13abbb"
  license "Apache-2.0"
  head "https://github.com/happyikas/Aegis-ATV.git", branch: "main"

  depends_on "python@3.11"
  depends_on "uv"
  depends_on "git"

  def install
    # Install the full source tree into the cellar — the hook needs to
    # import `aegis` and run `tools/aegis_local_hook.py` at every
    # Claude Code tool call, so a stripped entrypoint is not enough.
    libexec.install Dir["*"]

    # Pre-resolve and lock dependencies inside the cellar. hatchling
    # validates pyproject.toml's `readme = "README.md"` against the
    # project root during editable builds — this works *now* because
    # README.md / LICENSE / NOTICE were just installed alongside
    # pyproject.toml. (Homebrew strips them out *after* this method
    # returns; see `post_install` for the restoration that keeps
    # later builds working.)
    cd libexec do
      system Formula["uv"].opt_bin/"uv", "sync", "--frozen", "--no-dev"
    end

    # Direct venv shim — bypasses `uv run` (which would re-sync the
    # project on every invocation, including pulling dev deps and
    # rebuilding the editable wheel). The .venv was already populated
    # above; pointing the shim at the entry script is faster and side-
    # steps the metafile-validation problem entirely.
    (bin/"aegis").write <<~SHIM
      #!/usr/bin/env bash
      exec "#{libexec}/.venv/bin/aegis" "$@"
    SHIM
    chmod 0755, bin/"aegis"
  end

  def post_install
    # Homebrew auto-extracts README.md / LICENSE / NOTICE / CHANGELOG.md
    # from the install destination to prefix/ as "metafiles" between
    # `def install` returning and `def post_install` running. Anything
    # in the source tree that imports the project (e.g., `uv pip install
    # -e libexec` from a user's own venv, the firewall hook reading
    # `pyproject.toml`, or a future `uv sync` for a profile upgrade)
    # will fail without these files. Mirror them back into libexec so
    # the source tree remains self-consistent.
    %w[README.md LICENSE NOTICE CHANGELOG.md].each do |meta|
      cp(prefix/meta, libexec/meta) if (prefix/meta).exist?
    end
  end

  def caveats
    <<~EOS
      Aegis is installed but not yet wired into Claude Code.

      To activate the firewall hook for the current user:

        aegis install --mode local

      Then fully quit and relaunch Claude Code.

      Solo Free contract — by default, Aegis makes 0 cloud calls.
      All processing happens on this machine; ~/.aegis/audit.jsonl
      is the only state written.

      Documentation:
        #{libexec}/docs/PERSONAL_QUICKSTART.md
    EOS
  end

  test do
    # CLI is installed and version banner prints something non-empty.
    assert_match(/aegis/i, shell_output("#{bin}/aegis --version 2>&1", 0))

    # Run read-only subcommands under an isolated HOME so they cannot
    # touch the real ~/.claude/settings.json or ~/.aegis/audit.jsonl.
    ENV["HOME"] = testpath.to_s

    # `aegis report` against a fresh (non-existent) audit log should
    # exit cleanly with a friendly "no entries" banner — exercises the
    # CLI -> firewall import path without writing any state.
    system bin/"aegis", "report"
  end
end
