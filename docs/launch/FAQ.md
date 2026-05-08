# Launch FAQ — answers to anticipated questions

A working list of questions we expect at launch, with concise answers.
Use these as drafts in HN / Reddit / Twitter replies — copy, edit for
the specific phrasing of the question, do not paste verbatim.

The questions are ordered from "most likely to be asked first" to
"specialised but worth pre-answering."

---

## Trust & threat model

### "Can a determined attacker just bypass this?"

Aegis is defence in depth, not a single gate. The high-confidence
rules (step310 dangerous patterns, step311 cloud destructive) BLOCK
the obvious silent-exfil shapes that an LLM doesn't know to obfuscate.
A determined attacker controlling the LLM end can paraphrase around
individual regex rules — that is exactly why step340 reads the
structured ATV through an sLLM judge with a RAG-backed incident
corpus, not just keyword-match.

The audit chain ensures every decision is forensically recoverable.
"It slipped through" is detectable, not silent. There is no claim of
formal-proof completeness; the README is explicit on this.

### "What is the actual threat model?"

We assume:
* The local user is trusted.
* The LLM is honest but fallible — it can choose wrong actions when
  reasoning leads it to a destructive shortcut.
* The LLM's *inputs* (tool args, agent state) may be partially
  poisoned — by a tampered `CLAUDE.md`, an injected `.mcp.json`, or
  a prompt-injection attack on a downstream document.

We do **not** assume:
* That the local user is an attacker. (They have the keys; they can
  forge whatever they want.)
* That OS-level sandboxing is unavailable. Aegis layers on top.

### "What if I run Aegis as a non-trusted user?"

Out of scope for the Personal MVP. The sidecar mode (multi-tenant
FastAPI service) is the path for that case; it is documented but
deliberately not the default install.

---

## Performance & ergonomics

### "Won't the false-positive rate kill the dev experience?"

The `policies/safe_actions.json` allowlist (and
`policies/safe_bash_subcommands.json` for shell sub-commands) covers
~80% of normal coding-AI traffic — `ls`, `git status`, `pytest`,
`ruff`, `Read` / `Grep` / `Glob` — at <5 ms median, no judge call.

The 90-case macmini regression suite (`uv run python -m demo.macmini
all`) is the deterministic gate against new false positives. PRs
that add rules must keep the suite at 100/100.

### "What's the latency overhead per call?"

| Path | Median |
|------|--------|
| Safe-allowlist fast-path | 4 ms |
| Pattern-rule BLOCK | 8 ms |
| sLLM judge (`dummy`) | 12 ms |
| sLLM judge (`local-phi3`) | 180 ms |
| sLLM judge (`haiku`) | 420 ms |

For typical sessions this is <1% overhead.

### "Why not just use Claude Code's built-in approval?"

Claude Code's `--dangerously-skip-permissions` is binary — either ask
every time, or never. Aegis sits between them: known-safe ops
auto-allow, ambiguous gets a structured reason and a single approval
prompt, known-destructive BLOCKs outright. The signed audit chain is
the part you cannot get from the built-in.

### "Does this slow down agentic loops?"

Read-only loops (`Read` → `Grep` → `Read` …) hit the fast-path. Tool
calls that mutate state (Write, Edit, Bash) do go through the full
pipeline; that is the point. The loop detector (step336) explicitly
catches "same call 3× in a row" so you do not pay for repeated
ambiguous-judge calls in a runaway agent.

---

## Privacy & data

### "Does anything leave my machine?"

Default install (`aegis install --mode local`):
* **No outbound network calls.** You can confirm with `tcpdump` or
  Little Snitch.
* All processing happens on your machine.
* The only state written is `~/.aegis/audit.jsonl` (signed audit log)
  and `~/.aegis/keys/*` (Ed25519 signing key).

Optional opt-in:
* `AEGIS_JUDGE_PROVIDER=haiku` + `ANTHROPIC_API_KEY` → calls Anthropic.
* `AEGIS_EMBEDDING_PROVIDER=openai` + `OPENAI_API_KEY` → calls OpenAI.
* `AEGIS_EMBEDDING_PROVIDER=bge-local` → one-time GGUF download, then
  fully local.

### "Is the audit log readable as plain text?"

Yes — `~/.aegis/audit.jsonl` is one JSON object per line. The Ed25519
signature and SHA3-256 chain are fields on each record, not an opaque
binary blob. `aegis verify-audit` walks the chain; `aegis report`
aggregates the last session into a 5-line summary.

### "Can I export it / pipe it / archive it?"

Yes. The format is documented in `docs/PERSONAL_QUICKSTART.md` §
"audit log이 너무 큼". The 90-day archive recipe is a one-liner.

---

## Distribution & install

### "Why three install paths?"

Different audiences:
* **`git clone` + `uv sync`** — for contributors who want the full
  dev environment.
* **`curl | bash`** — for users who want zero-clone install. Idempotent,
  refuses root, clones into `~/.aegis-src`.
* **Homebrew tap** — for users who already manage software via
  `brew`. Same end state, brew-managed cellar.

All three end at the same in-process hook + same `~/.claude/settings.json`
patch + same audit log path.

### "Brew formula has an all-zero sha256 — is this a placeholder?"

Yes. The formula ships at HEAD with a placeholder sha256 — `brew tap`
succeeds and `brew install` fails the integrity check. The formula
bumps to a real sha256 at the next signed release tag (procedure
documented in `pkg/brew/README.md`). Until then, prefer the
`curl | bash` or git-clone path.

### "Will this graduate to homebrew-core?"

That's the plan. The remaining criteria are 30 days of release
history with no breaking changes plus a network-free test block —
the LICENSE prerequisite is now satisfied (Apache-2.0). The full
checklist is in `pkg/brew/README.md`.

### "Windows support?"

Not yet. Native macOS + Linux today. WSL2 should work via the Linux
path, untested by CI. If you try it and it works/breaks, please
open an issue.

---

## Architecture & design

### "Why a 2,080-D float32 vector?"

The ATV-2080-v1 is the patent-aligned schema (30 named sub-fields ×
varying widths = 2,080 floats). It is the structured representation
that lets the firewall, the sLLM judge, the loop detector, and the
RAG corpus all read the same shape.

The 200-D hardware band (indices 1880..2079) is intentionally
zero-filled in T2 (the software MVP); that's the T3 hardware-emulation
work in `step337_hw_anomaly`.

### "Why Ed25519 + SHA3-256 + Merkle?"

Ed25519: deterministic signing, fixed-size signatures, well-audited
crypto. SHA3-256: NIST-standardized, no length-extension issues.
Together they give a Merkle-chained, append-only log where any
mutation breaks every subsequent recompute.

Practical consequence: `aegis verify-audit` catches not only edits but
also re-orderings — moving line N before line N-1 fails the chain
just like editing the JSON would.

### "Why an sLLM judge instead of pure rules?"

Pure rules cover the high-confidence cases (step310 / step311) at
near-zero false positives. The grey area between "obviously safe"
and "obviously destructive" is where the LLM judge earns its keep —
deciding whether `find /var/log -name '*.gz' -delete` is log
rotation or data destruction needs context the regex doesn't have.

For users who want zero LLM calls: `--judge dummy` is a deterministic
keyword-only judge. The trade-off is some grey-area calls go to
REQUIRE_APPROVAL where a heavier judge would have ALLOWed.

### "Why ATV / firewall / Ed25519 etc — is there a research paper?"

The architecture corresponds to AegisData patent v4. The README's
"What's in the box" table maps each milestone (M1–M17) to the
specific patent claim it implements. That's the canonical reference;
there is no separate paper at this time.

---

## Maintenance & future

### "Who's maintaining this?"

Solo project today. Contributions welcome under `CONTRIBUTING.md`;
security issues via `SECURITY.md`. The "lift" required to take it
in a new direction is documented in `pkg/brew/README.md` § graduation,
`docs/PERSONAL_QUICKSTART.md` § next steps, and the issue tracker.

### "How often will rules be updated?"

The detection-rule template (`.github/ISSUE_TEMPLATE/detection_rule.yml`)
is the most reliable contribution path. Rules ship with their incident
reference and false-positive analysis. PRs that pass the 90-case
macmini suite and add fixtures for the new pattern land within the
week.

### "Plans for monetization?"

Personal MVP stays free / open-source. Sidecar mode (multi-tenant
FastAPI service with M14 quarantine, M15 encrypted journal, M16 HAM)
is the platform for team / enterprise use; pricing model TBD,
explicitly *not* a constraint on the Personal install path.

---

## Misc / nice to have

### "Can I use this with [non-Claude] agents?"

Today: only Claude Code (it's the only agent runtime we have a
PreToolUse hook for). MCP server packaging is the planned path
to "use Aegis from any MCP-aware agent" — see roadmap in the
blog post.

### "Can I disable specific rules?"

Yes — edit `policies/rag_corpus/rules.jsonl` to remove the rule, or
add an entry to `policies/safe_actions.json` to fast-path-allow a
specific shape. Document the *why* in the commit message; the rule
catalog is small enough that drift compounds quickly otherwise.

### "Can I see what got caught last session?"

`uv run aegis report` — 5-line summary.
`uv run aegis verify-audit` — chain integrity.
`tail ~/.aegis/audit.jsonl | jq` — raw structured records.
