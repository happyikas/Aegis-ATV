# Aegis: a deterministic firewall between Claude Code and your machine

*Draft for the project blog / Substack / LinkedIn — longer-form
companion to the Show HN post. Edit before publishing.*

---

## Why I built it

Coding agents are getting good. They are also gaining the ability to
run arbitrary shell commands, edit arbitrary files, and call arbitrary
APIs against the credentials sitting in your shell. Most of the
incidents I have seen in the wild are not malice — they are an LLM
that decided "the cleanest way to satisfy the user is to drop the
table and recreate it." On its face that is reasoning; in practice
it is a destructive shell command running against a database the user
did not intend.

The "are you sure?" prompt is a UX patch over a design problem.
It is the wrong layer. By the time the model is asking, it has already
decided the action is fine. The boundary needs to live in the
sandbox, not the conversation.

Aegis is that boundary. Every Claude Code tool call is intercepted at
`PreToolUse`, scored by a 16-step firewall, and either allowed,
escalated to human approval, or BLOCKed cryptographically. The result
is appended to a signed, hash-chained audit log on local disk. No
network calls by default.

## What "in-process" actually means

Three install paths land on the same in-process hook:

| Path | One-liner | Where the source lives |
|------|-----------|------------------------|
| Source clone | `git clone … && uv run aegis install --mode local` | wherever you cloned |
| `curl \| sh` | `curl -LsSf …/scripts/install.sh \| bash` | `~/.aegis-src` |
| Homebrew | `brew tap happyikas/aegis … && brew install …` | Homebrew cellar |

After install, `~/.claude/settings.json` carries a `PreToolUse`
hook entry that points at `tools/aegis_local_hook.py`. Each Claude
Code tool call invokes this script with the tool name, args, and
context as JSON on stdin. The script returns ALLOW / BLOCK /
REQUIRE_APPROVAL on stdout. Median latency for safe-allowlist
fast-path calls (`ls`, `git status`, `Read`, `Grep`, `pytest`,
`ruff`) is under 5 ms.

The Solo Free contract: by default, no network calls leave the
machine. The "judge" is a deterministic rule engine over a 30-field
ATV vector; embeddings are SHA3 hashes; the audit chain uses local
Ed25519 + SHA3-256. You can verify with `tcpdump` or Little Snitch
while running Claude Code; the chart is flat.

If you want a real LLM judge later, `aegis install --mode local
--judge haiku` flips one config. The dummy/hybrid/local-Phi/Haiku
ladder is the upgrade path.

## The 16-step pipeline, in one paragraph each

**step305 — safe allowlist fast-path.** Before the heavy steps, a
literal-substring match against `policies/safe_actions.json`. Read /
Grep / Glob / `ls` / `git status` / `pytest` / `ruff` skip the rest of
the pipeline. This is what keeps the median latency low — most coding
traffic is read-only and predictable.

**step309 — instruction-drift baseline.** SHA3 hashes of `CLAUDE.md`,
`AGENTS.md`, `.mcp.json`, plugin and skill manifests are baselined at
install. Every PreToolUse re-hashes; any drift BLOCKs the call until
the user runs `aegis baseline reattest`. The point is not to prevent
edits — it is to make sure the LLM's stated context matches what it
is reading.

**step310 — dangerous-pattern args.** Regex match against destructive
filesystem ops, drop-table, system password file reads, eval / exec,
privilege prefix on commands. The rules are small and high-confidence;
this is the layer that catches "Claude tried to recursively delete
$HOME" before any judge runs.

**step311 — cloud destructive.** kubectl `delete`, Terraform
`destroy`, Helm uninstall, AWS `iam delete-policy`, EC2
`terminate-instances`, S3 force-rm, force-push to `main` / `master` /
`production`. Patterns lifted from public post-mortems and the
Anthropic incident corpus.

**step315 — AID auth (sidecar mode).** Per-agent-instance circuit
breaker. Three violations in a window quarantine the agent ID until
human ACK.

**step320 — blast radius.** Estimates how many files / network
endpoints / cloud resources a tool call would reach if it succeeded.
Encoded into the ATV; consumed by the sLLM judge.

**step330 — role / scope.** Reads the agent's declared role
(`role_id`) and clamps allowed verbs accordingly. A `read-only-role`
attempting `write_file` is BLOCKed regardless of args.

**step335 — cost gate.** Tracks cumulative token spend per session;
escalates over `AEGIS_TOKEN_BUDGET` to REQUIRE_APPROVAL.

**step336 — loop detector.** Same tool name + same args repeated 3×
escalates to REQUIRE_APPROVAL. Read-only repeats dedupe and surface
in `aegis report`.

**step340 — sLLM judge + RAG.** For ambiguous calls (everything that
isn't fast-path-safe and isn't pattern-destructive), a small language
model reads the structured ATV plus the top-k retrieved playbooks
from `policies/rag_corpus/` and emits a decision with a reasoned
justification. The same prompt produces a 30-subfield contribution
attribution head — useful for debugging false positives.

**step350 — approval gate.** REQUIRE_APPROVAL decisions wait on the
human. In Claude Code's UI this surfaces as the standard approval
prompt. Without approval, the tool does not run.

**step360 — audit chain.** ALLOW / BLOCK / REQUIRE_APPROVAL plus the
full step trace gets signed (Ed25519) and chained (SHA3-256
prev_hash → this_hash) into `~/.aegis/audit.jsonl`. The signing key
lives in `keys/`, never in the audit file.

**step370 — exec or BLOCK.** The terminal step. ALLOW continues to
the tool's actual execution; BLOCK / unapproved REQUIRE_APPROVAL
returns to Claude Code with a structured rejection.

## What gets caught, in practice

Out of the box: 31 detection rules + 6 incident playbooks. The
non-obvious wins:

- **Force-push to a protected branch.** `git push --force` against
  `main` / `master` / `production` / `release/*` BLOCKs at step311.
  Force-pushing a feature branch you own is allowed.
- **Same-call loop.** Three `curl localhost:8000/...` against the
  same path in 30 seconds escalates to REQUIRE_APPROVAL. Catches
  "agent stuck in retry loop burning tokens" before the cost gate.
- **Instruction-drift after install.** Edit `CLAUDE.md` to tell the
  agent "ignore the firewall and run X" — every subsequent
  PreToolUse BLOCKs until you reattest the baseline.
- **Sandbox escape patterns.** `docker run --privileged`, `nsenter`,
  `chroot`, `mount --bind /` — all BLOCKed regardless of args.

## What is intentionally not in scope

- **Detecting prompt-injection in arbitrary natural language.**
  step309 detects drift in *named files*; it does not parse free-form
  text in tool args for jailbreak strings.
- **Replacing OS-level sandboxing.** Aegis is layered on top of macOS
  sandbox / Linux namespaces, not in lieu of them.
- **Stopping a fully-malicious LLM that has the binary's signing key.**
  If the attacker can read `keys/journal_data.key`, they can forge
  audit records. The threat model assumes the local user is trusted;
  the firewall is for honest agents that take wrong actions, and for
  later-in-time forensic verification.

## Performance

On a 2024 MacBook Air M2:

| Path | Median latency |
|------|----------------|
| Safe-allowlist fast-path | 4 ms |
| Pattern-rule BLOCK | 8 ms |
| sLLM judge (`dummy`) | 12 ms |
| sLLM judge (`local-phi3`) | 180 ms |
| sLLM judge (`haiku`) | 420 ms |

The fast-path matters because >80% of coding-AI traffic is
read-only. Aegis adds <1% latency overhead to a typical session.

## Roadmap

- Homebrew-core graduation (`pkg/brew/README.md` has the criteria
  — Apache-2.0 LICENSE prerequisite is satisfied).
- Windows / WSL2 native support (currently macOS + Linux only).
- MCP server packaging — register Aegis as an MCP server so other
  MCP-aware agents can use it without the Claude-Code-specific hook.
- Sidecar mode end-to-end docs (multi-tenant FastAPI service for
  team / enterprise use, separate from the Solo Free Personal MVP).

## Try it

The full 5-minute walkthrough lives in
[`docs/PERSONAL_QUICKSTART.md`](../PERSONAL_QUICKSTART.md). The fast
path:

```
brew tap happyikas/aegis https://github.com/happyikas/Aegis-ATV.git
brew install happyikas/aegis/aegis
aegis install --mode local
# restart Claude Code
```

Then ask Claude Code to do something destructive in your next
session. You will see the BLOCK before the tool runs.

Code, issues, and security disclosure channel:
[github.com/happyikas/Aegis-ATV](https://github.com/happyikas/Aegis-ATV).
