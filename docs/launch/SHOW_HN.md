# Show HN draft — Aegis

A pre-publish draft of the Show HN submission. Edit before posting; do
not submit verbatim. The form takes a separate **Title** and **Text**;
both are below.

---

## Title

> Show HN: Aegis – an in-process firewall for Claude Code's tool calls

(80-char limit on HN. Alternates if the above is too dense:
*"Show HN: Aegis – BLOCKs destructive Claude Code tool calls before they run"*,
*"Show HN: A 16-step firewall + signed audit log for Claude Code agents"*.)

## URL

`https://github.com/happyikas/Aegis-ATV`

## Text

> Hi HN. I built Aegis because I kept watching Claude Code run things
> I didn't want it to — recursive deletes against the wrong path, force
> pushes to protected branches, drop-table on the wrong database — and
> "are you sure?" prompts felt like a UX patch over a design problem.
>
> Aegis is a small Python service that hooks Claude Code's
> `PreToolUse` callback and runs every tool call through a 16-step
> firewall before deciding ALLOW / REQUIRE_APPROVAL / BLOCK. Three
> things make it different from a glorified regex list:
>
> 1. **Fully on-device by default.** A Solo Free install (`aegis
>    install --mode local`) makes 0 cloud calls — the firewall, the
>    sLLM judge, and the audit chain all run in-process. You can
>    confirm with `tcpdump`. Optional opt-in to Anthropic Haiku /
>    OpenAI is one flag away if you want a heavier judge.
>
> 2. **Decision is signed and chained.** Every verdict is appended to
>    `~/.aegis/audit.jsonl` with an Ed25519 signature and a SHA3-256
>    `prev_hash` / `this_hash` pair. `aegis verify-audit` walks the
>    chain — a single mutated record breaks every subsequent recompute,
>    so silent edits and re-orderings both fail.
>
> 3. **The "what" is structured, not just a string.** Every call is
>    encoded into a 2,080-D float32 vector (ATV-2080-v1, 30 named
>    sub-fields covering blast radius, cost, role, prompt-injection
>    likelihood, etc.). The sLLM judge, the loop detector, and the
>    RAG-backed playbook system all read the same structured
>    representation.
>
> Three install paths, all under 5 minutes:
>
>     git clone + uv sync + aegis install --mode local
>     curl -LsSf https://raw.githubusercontent.com/happyikas/Aegis-ATV/main/scripts/install.sh | bash
>     brew tap happyikas/aegis https://github.com/happyikas/Aegis-ATV.git && brew install happyikas/aegis/aegis
>
> What gets caught out of the box (31 rules + 6 incident playbooks):
> filesystem purges against system paths, force-push to main / master /
> production, Kubernetes `delete`, Terraform `destroy`, AWS IAM
> mutations, EC2 terminate, S3 force-rm, drop-table on production
> tables, unbounded SQL deletes, sandbox-escape via privileged Docker
> / nsenter / chroot, prompt-injection patterns ("ignore previous
> instructions", `[INST] system`, MCP-injection), reads of cloud
> credentials / SSH private keys, instruction drift on
> `CLAUDE.md` / `AGENTS.md` / `.mcp.json`, and same-call-3× loop
> detection.
>
> The README has a 32-second GIF and a 5-minute walkthrough; I'm
> happy to dive into any of: the 30-subfield ATV schema, the audit
> chain crypto, the sLLM judge prompt structure, the safe-allowlist
> design, or the trade-off between dummy / hybrid / real-LLM modes.
>
> Caveat: this is solo-built and the LICENSE file isn't in yet — the
> repo is open and contributions are welcome under
> [CONTRIBUTING.md](https://github.com/happyikas/Aegis-ATV/blob/main/CONTRIBUTING.md);
> security findings go through [SECURITY.md](https://github.com/happyikas/Aegis-ATV/blob/main/SECURITY.md).

---

## Posting checklist (do not skip)

- [ ] Read [pg's Show HN guidelines](https://news.ycombinator.com/showhn.html)
      — the post must offer something the reader can try.
- [ ] Verify all three install paths still work end-to-end on a clean
      VM today (`docs/PERSONAL_QUICKSTART.md` § TL;DR).
- [ ] Check that `Formula/aegis.rb`'s `sha256` is **not** the all-zero
      placeholder before linking the brew path. If it still is, drop
      the brew bullet from the post or land
      [pkg/brew/README.md § release procedure](../../pkg/brew/README.md)
      first.
- [ ] Confirm CI is green on `main` and the
      [tests-passed badge](../../README.md#aegis--action-firewall-for-claude-code)
      number is current.
- [ ] Sanity-check the GIF renders inline on a fresh GitHub
      cache-bust (`?v=$(date +%s)`).
- [ ] Pre-write three of your own answers to the FAQ — Hacker News
      reads "no follow-up after 2 hours" as "abandoned".
- [ ] Post Tue/Wed/Thu 09:00 PT. Avoid US holidays + earnings days
      (heavier signal-to-noise loss in the morning queue).
- [ ] First-comment etiquette: do not comment in the first 30 min;
      let the post stand on its own. Then engage every reply.

## Reply templates for the predictable questions

**"What stops a determined attacker from just bypassing this?"**

> Aegis is a defence-in-depth boundary, not a single gate. The
> high-confidence rules (step310 `\brm\s+-rf\s+/`, step311
> `force-push.*\bmain\b`) BLOCK silent exfil tools the LLM doesn't
> know to obfuscate. Determined attackers who control the LLM end
> can paraphrase around individual rules — that's why step340 reads
> the structured ATV through an sLLM judge with a RAG-backed
> incident corpus, not just keyword-match. The audit chain ensures
> that every decision is forensically recoverable; "it slipped
> through" is detectable, not silent. There is no claim of
> formal-proof completeness; the README is explicit about that.

**"Won't the false-positive rate kill the dev experience?"**

> The safe-allowlist fast-path (`policies/safe_actions.json`,
> `safe_bash_subcommands.json`) covers ~80% of normal coding-AI
> traffic — `ls`, `git status`, `pytest`, `ruff`, `Read` /
> `Grep` / `Glob` — at <5 ms median, no judge call. Only ambiguous
> calls hit the sLLM judge. The 90-case macmini regression suite
> (`uv run python -m demo.macmini all`) is the deterministic gate
> against new false positives; PRs that add rules must keep it at
> 100/100.

**"Why not use Claude Code's built-in approval prompts?"**

> The built-in `--dangerously-skip-permissions` is a *binary* —
> either Claude Code asks every time, or never. Aegis sits between
> them: known-safe ops auto-allow, ambiguous gets a structured
> reason and a one-button approval, known-destructive BLOCKs
> outright. The audit chain is the part you can't get from the
> built-in.

**"Brew formula: why the all-zero sha256?"**

> The formula ships at HEAD with a placeholder sha256 — `brew tap`
> succeeds and `brew install` fails the integrity check. The
> formula bumps to a real sha at the next signed release tag (see
> `pkg/brew/README.md` for the procedure). For now, prefer the
> `curl | bash` or git-clone path.

## Lead screenshots to attach (in order)

1. `demo/recording/quickstart.gif` — README first-screen GIF.
2. `screens/01b-dashboard-with-state.png` — only if you'll show the
   sidecar-mode dashboard. Skip for a Personal-MVP-only post.
3. `aegis report` actual session output —
   `docs/launch/dogfooding/01-aegis-report.txt`
   (PR 6 in the launch-blocker punchlist captures these).
4. `aegis verify-audit` showing chain integrity —
   `docs/launch/dogfooding/02-aegis-verify-audit.txt`.
