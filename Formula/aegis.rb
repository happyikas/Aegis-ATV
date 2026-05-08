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
  url "https://github.com/happyikas/Aegis-ATV/archive/refs/tags/v4.4.0.tar.gz"
  # NOTE: sha256 placeholder — when cutting a release, regenerate via:
  #   curl -sL https://github.com/happyikas/Aegis-ATV/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
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

    # Pre-resolve and lock dependencies inside the cellar.
    cd libexec do
      system Formula["uv"].opt_bin/"uv", "sync", "--frozen", "--no-dev"
    end

    # Tiny shim that forwards `aegis` to the uv-managed venv.
    (bin/"aegis").write <<~SHIM
      #!/usr/bin/env bash
      exec "#{Formula["uv"].opt_bin}/uv" run --project "#{libexec}" aegis "$@"
    SHIM
    chmod 0755, bin/"aegis"
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
