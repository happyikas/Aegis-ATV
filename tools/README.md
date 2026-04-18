# `tools/` — Claude Code integration

## `aegis_hook.py`

A `PreToolUse` hook that fires before every tool call inside Claude Code,
sends an ATV evaluation request to the running AegisData service, and
short-circuits the tool with stderr if the verdict is `BLOCK` (or
`REQUIRE_APPROVAL`, by default).

Stdlib-only Python — no `pip install` needed in the Claude Code shell.

### Install (3 steps)

1. Make sure the Aegis service is running:
   ```bash
   docker compose up -d        # from the repo root
   curl -sf http://localhost:8000/healthz   # → {"ok": true, ...}
   ```

2. Find the absolute path of `aegis_hook.py`:
   ```bash
   realpath tools/aegis_hook.py
   # /Users/you/.../MVP/tools/aegis_hook.py
   ```

3. Add this to `~/.claude/settings.json` (create the file if missing):
   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "*",
           "hooks": [
             {
               "type": "command",
               "command": "python3 /ABSOLUTE/PATH/TO/MVP/tools/aegis_hook.py"
             }
           ]
         }
       ]
     }
   }
   ```

   Restart Claude Code. From now on every tool call goes through Aegis.

### Verify it works

```bash
bash tools/test_hook.sh
# 10 passed, 0 failed
```

Or in Claude Code itself, ask the agent to do something obviously bad
(`run rm -rf /`) — you should see the hook output:

```
[aegis-hook] BLOCK  Bash  atv=4d432a97
           reason: dangerous pattern: \brm\s+-rf\s+/
```

…and Claude will not run the command.

### Tool-name mapping

Claude Code's built-in tools are mapped to Aegis's tool taxonomy so that
the firewall's blast-radius lookup (step 320) makes sense:

| Claude Code tool | Aegis tool | blast |
|---|---|---|
| `Read`, `Grep`, `BashOutput`, `ExitPlanMode`, `ListMcpResources`, `ReadMcpResource` | `read_file` | 1 |
| `Glob` | `list_directory` | 1 |
| `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `TodoWrite` | `write_file` | 3 |
| `Bash`, `KillShell` | `execute_shell` | 8 → REQUIRE_APPROVAL |
| `WebFetch`, `WebSearch`, `Task` | `call_external_api` | 5 |

Anything not in this table defaults to `call_external_api` (blast 5).
This means **`Bash` is REQUIRE_APPROVAL by default** — if you'd rather
let it through with just a warning, set `AEGIS_APPROVE_AS_BLOCK=0`.

### Environment variables

| Var | Default | Meaning |
|---|---|---|
| `AEGIS_URL` | `http://localhost:8000` | Where the Aegis service is reachable |
| `AEGIS_TENANT_ID` | `claude-code` | Tagged on every audit record |
| `AEGIS_HOOK_TIMEOUT` | `5` | `/evaluate` HTTP timeout (seconds) |
| `AEGIS_FAIL_OPEN` | `0` | If `1`, allow the tool when Aegis is unreachable. Default fails closed. |
| `AEGIS_APPROVE_AS_BLOCK` | `1` | If `0`, REQUIRE_APPROVAL → exit 0 with stderr warning instead of blocking. CLI hooks can't show interactive approval, so the default treats APPROVAL like BLOCK for safety. |
| `AEGIS_HOOK_VERBOSE` | `0` | If `1`, also log ALLOW verdicts (useful for debugging). |

You can put these in `~/.claude/settings.json` per-hook:

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "AEGIS_URL=http://localhost:8000 AEGIS_HOOK_VERBOSE=1 python3 /abs/path/aegis_hook.py"
      }]
    }]
  }
}
```

### Narrowing the matcher

`"*"` runs the hook on every tool. To skip read-only tools and only
guard side-effecting ones:

```json
"matcher": "Bash|Edit|MultiEdit|Write|NotebookEdit|WebFetch|Task"
```

### Inspecting what Aegis saw

Every hook firing creates an audit record signed by Ed25519 and chained
into the Merkle log. List them via the dashboard or API:

```bash
curl -s http://localhost:8000/audit/claude-code-XXXXXXXX | jq '.length, .chain_valid'
```

…or open <http://localhost:8000/> and paste your aid into the audit
panel.

### Uninstall

Remove the `PreToolUse` block from `~/.claude/settings.json` and restart
Claude Code.
