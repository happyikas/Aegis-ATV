# Dogfooding artifacts — captured from a real session

Real CLI output from a working install, captured against
`~/.aegis/audit.jsonl` after ~one development week of normal Claude
Code use. Replaces the synthetic GIF stills with proof-of-life
evidence that the firewall actually fires on real coding traffic.

Numbers and timestamps are real. Personal identifiers (the AID UUID,
the macOS username path) have been redacted; everything else —
trace IDs, atv_sha3 hashes, Merkle prev_hash/this_hash, m13
attribution scores, latencies, decision counts — is verbatim.

## What's here

| File | Captured by | What it shows |
|------|-------------|---------------|
| [`01-aegis-report.txt`](01-aegis-report.txt) | `uv run aegis report` | The 5-line risk summary printed at session level. 5,581 records → 122 ALLOW, 2,374 REQUIRE_APPROVAL, 29 BLOCK. |
| [`02-aegis-verify-audit.txt`](02-aegis-verify-audit.txt) | `uv run aegis verify-audit` | Chain integrity check. `5,583 records intact` — every prev_hash/this_hash verified end-to-end. |
| [`03-block-record.json`](03-block-record.json) | `grep -m1 '"decision": "BLOCK"' ~/.aegis/audit.jsonl \| jq` | One full BLOCK record showing the complete step trace (305→340), the 30-subfield m13 attribution head's top contributors, the ATV sha3, and the Merkle hashes. |
| [`04-require-approval-record.json`](04-require-approval-record.json) | same, for `REQUIRE_APPROVAL` | Shorter trace example — m13 score in the [0.40, 0.70) range escalates to human approval rather than outright BLOCK. |
| [`05-block-summary.txt`](05-block-summary.txt) | `jq` over the 29 BLOCKs | Distinct (tool, reason) pairs from the session. Shows step310/311 catching `\brm\s+-rf\s+/`, `DROP\s+TABLE`, `/etc/(shadow\|passwd)`, sudo, exec/system, sensitive-path reads (`~/.aws/credentials`), and m13-attribution-driven BLOCKs. |
| [`06-latency-stats.txt`](06-latency-stats.txt) | per-decision-type latency aggregation | p50 / p95 / p99 by decision. ALLOW p50 ≈ 33 ms (full pipeline; safe-allowlist fast-path is faster but underrepresented in this set since most ALLOWs went via the heavier m13 attribution head). |

## Why these and not Claude Code UI screenshots?

UI screenshots are not reproducible from inside a CLI session — the
Claude Code window is a separate desktop app. Capturing the actual
`⛔ BLOCK` banner that appears in Claude Code's UI when the firewall
denies a tool call requires:

1. macOS / Linux desktop with Claude Code running.
2. A live ambiguous-or-destructive prompt that triggers a BLOCK.
3. `cmd-shift-4` (macOS) or equivalent screenshot capture.

That step is best done by the user **immediately before posting**,
so the screenshot reflects the current Claude Code UI version. The
artifacts here capture what's stable and reproducible — the CLI
output and the audit-log structure.

When you do capture the UI BLOCK shot, save it as
`07-claude-code-block.png` in this directory and reference it from
[`../SHOW_HN.md`](../SHOW_HN.md) § Lead screenshots.

## Re-capturing these

After landing significant firewall changes, or before the next
launch refresh:

```bash
# 1. Have a real audit log (any non-trivial Claude Code session
#    against an installed hook will do).

# 2. Re-generate the artifacts.
mkdir -p docs/launch/dogfooding
uv run aegis report     > docs/launch/dogfooding/01-aegis-report.txt
uv run aegis verify-audit 2>&1 \
  | sed 's/\x1b\[[0-9;]*m//g'  \
  > docs/launch/dogfooding/02-aegis-verify-audit.txt

# Sanitize the username path before committing
sed -i.bak "s|/Users/$(whoami)|/Users/example|g" \
  docs/launch/dogfooding/01-aegis-report.txt
rm docs/launch/dogfooding/01-aegis-report.txt.bak

# 3. Pull a representative BLOCK record + REQUIRE_APPROVAL record,
#    redacting AID UUIDs (random per-session, but mask anyway).
python3 - <<'PY'
import json, pathlib
out_dir = pathlib.Path('docs/launch/dogfooding')
mask = 'claude-code-session:00000000-0000-0000-0000-000000000000'
seen = {'BLOCK': False, 'REQUIRE_APPROVAL': False}
files = {'BLOCK': '03-block-record.json',
         'REQUIRE_APPROVAL': '04-require-approval-record.json'}
with open(pathlib.Path.home() / '.aegis' / 'audit.jsonl') as f:
    for line in f:
        r = json.loads(line)
        d = r.get('decision')
        if d in seen and not seen[d]:
            r['aid'] = mask
            (out_dir / files[d]).write_text(json.dumps(r, indent=2))
            seen[d] = True
        if all(seen.values()):
            break
PY
```

## Privacy contract

Before committing any file in this directory:

- [ ] No `.env` content, API keys, or `keys/*.pem` filenames visible
- [ ] No real customer / production tool args (the demo audit log
      should be from `claude-code-local` or a synthetic AID)
- [ ] Username path replaced with `/Users/example` or `/home/example`
- [ ] AID UUID masked (any identifier matching the
      `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` pattern in `aid` fields)
- [ ] Any tool args containing repository-specific file paths
      reviewed line-by-line
