# `aegis uninstall` + `--explain --json` 가이드

> 두 가지 polish UX 결합:
>
> 1. `aegis uninstall` — settings.json 자동 cleanup (PR #20 의 install
>    inverse)
> 2. `aegis report --explain --json` — CI / jq 친화적 구조화 출력

---

## 1. `aegis uninstall`

### 1.1 한 줄 사용법

```bash
uv run aegis uninstall                # backup 생성 + 모든 Aegis hook 제거
uv run aegis uninstall --dry-run       # 어떤 항목이 제거될지 미리보기
uv run aegis uninstall --no-backup     # backup 생략 (CI 자동화용)
```

### 1.2 동작

PR #28 의 `_drop_aegis_entries` 핑거프린트 (`aegis_local_hook.py`,
`aegis_hook.py`, `tools/hooks/post_tool.py`,
`tools/hooks/session_end.py`) 를 사용해서 정확히 install 의 inverse:

| 항목 | 처리 |
|---|---|
| Aegis-owned PreToolUse / PostToolUse / Stop | ✅ 제거 |
| 사용자가 설치한 third-party (prettier, gitleaks 등) | ❌ 보존 |
| 빈 hook stage (`PreToolUse: []`) | ❌ 보존 (Claude Code 가 허용) |
| 다른 settings.json 키 (theme, enabledPlugins 등) | ❌ 보존 |

**Idempotent:** Aegis hook 이 없는 settings.json 에서 실행해도 OK
(green "nothing to remove" + exit 0).

### 1.3 출력 예시

```
$ uv run aegis uninstall --dry-run
[uninstall] settings.json: /Users/chanikpark/.claude/settings.json
[uninstall] would remove 3 Aegis-owned hook entry(ies):
    PreToolUse                AEGIS_EMBEDDING_PROVIDER=bge-local AEGIS_JUDGE_PROVIDER=hybrid…
    PostToolUse               PYTHONPATH=…/src python3 …/tools/hooks/post_tool.py
    Stop                      python3 …/tools/hooks/session_end.py

dry-run — settings.json NOT modified
```

```
$ uv run aegis uninstall
... (same preview as above)
backed up existing settings → settings.json.bak.1735698123
✓ removed 3 Aegis hook entry(ies) from /Users/chanikpark/.claude/settings.json

Restart Claude Code for the change to take effect.
  Per-session state at ~/.aegis/audit.jsonl, ~/.aegis/sessions/,
  ~/.aegis/shadow.jsonl is preserved — delete manually if you want
  a fully clean slate (or keep them for `aegis verify-audit`).
```

### 1.4 사용자 데이터 보존

uninstall 은 **hook 만** 제거. 다음은 manually 삭제 (또는 보존):

| 데이터 | 위치 | 권장 |
|---|---|---|
| 결정 audit log | `~/.aegis/audit.jsonl` (+ rotations) | 보존 (compliance) |
| 세션 drift state | `~/.aegis/sessions/` | 삭제 OK (대부분 일주일 후 자동 evict) |
| Burn-in shadow log | `~/.aegis/shadow.jsonl` | 보존 (M13 v3 학습 자료) |
| 다운로드 모델 | `./models/*.gguf` | 보존 (재설치 시 재사용) |
| Case memory | `./models/case_memory_v1.npz` | 보존 |

완전히 clean slate 원하면:

```bash
uv run aegis uninstall
rm -rf ~/.aegis/                       # 모든 audit + session 제거
rm -rf models/*.gguf models/*.npz      # 모든 모델 + memory 제거
```

---

## 2. `aegis report --explain --json`

### 2.1 한 줄 사용법

```bash
# 가장 최근 결정의 explain 을 JSON 한 줄로
uv run aegis report --explain LAST --json | jq '.'

# trace_id 로 특정 결정
uv run aegis report --explain abc123 --json | jq .explain.m13_top
```

### 2.2 출력 schema

JSON 한 줄 = **audit record 그 자체** (top-level 필드 + `explain` block):

```json
{
  "ts_ns": 1735698012345678901,
  "tool": "Edit",
  "aid": "sess-abc123",
  "decision": "BLOCK",
  "reason": "credential_pattern matched",
  "trace_id": "cad70ac33ecaa7b8...",
  "latency_ms": 14.3,
  "mode": "local",
  "explain": {
    "atv_dim": 2080,
    "atv_sha3": "6f09775f957d73df...",
    "step_traces": {
      "aegis.firewall.step310_args.run": "step310: hit credential_pattern",
      "aegis.firewall.step340_policy.run": "step340: BLOCK conf=0.95"
    },
    "m13_top": [
      {"subfield": "tool_arg_inspection", "score": 0.95},
      {"subfield": "output_content_fingerprint", "score": 0.70}
    ],
    "m13_score": 0.95,
    "rag": {
      "n_retrieved": 3,
      "top_cos": 0.79,
      "top_label": "BLOCK",
      "top_text": "agent committing AWS_SECRET to .env"
    },
    "session_drift": {
      "topic_drift": 0.42,
      "max_drift": 0.55,
      "n_calls": 7
    }
  },
  "prev_hash": "...",
  "this_hash": "..."
}
```

Schema 안정성: top-level 필드는 `local_chain.append` 의 contract,
`explain` 은 PR #26 의 contract. 둘 다 backwards-compatible 약속 —
새 필드 추가 가능, 기존 필드 제거 안 됨.

### 2.3 jq 패턴 예시

```bash
# 최근 BLOCK 결정의 top contributor
uv run aegis report --explain LAST --json | jq -r '.explain.m13_top[0].subfield'

# 특정 결정의 RAG retrieval 결과
uv run aegis report --explain abc123 --json | jq '.explain.rag'

# Drift 가 0.5 넘는 결정인지 확인 (CI gate)
uv run aegis report --explain LAST --json | \
  jq -e '.explain.session_drift.topic_drift > 0.5' \
  && echo "session has drifted significantly"

# 결정의 ATV SHA3 (replay 용)
uv run aegis report --explain LAST --json | jq -r '.explain.atv_sha3'

# 하루 audit 의 모든 BLOCK trace_id 와 reason
tail -1000 ~/.aegis/audit.jsonl | jq -c 'select(.decision=="BLOCK") | {trace_id, reason}'
```

### 2.4 에러 envelope (CI 친화적)

trace_id 매치 안 될 때:

```json
{
  "error": "not_found",
  "target": "no-such-trace",
  "audit_path": "/Users/chanikpark/.aegis/audit.jsonl"
}
```

Exit code 1, but JSON 항상 출력 — bash 의 `set -euo pipefail` 환경
에서도 안전.

---

## 3. dogfood `[15]` install ↔ uninstall roundtrip

```bash
./scripts/dogfood_check.sh
```

```
[15] Install ↔ uninstall roundtrip  (sandboxed)
  ✓ round-trip clean: 0 Aegis hooks remain, 1 third-party hook preserved
✓ 14/14 checks passed — green-light for real Claude Code
```

샌드박스 `$HOME` 으로 install → uninstall → 검증:
1. Pre-existing prettier hook 시작
2. install 실행 → 사용자 hook + Aegis hook 같이 존재
3. uninstall 실행 → Aegis hook 다 제거
4. **사용자 prettier hook 그대로 존재** (key invariant)

---

## 4. 누적 10개 PR

```
PR #20  install plumbing
PR #21  Llama-3.2-1B sLLM judge
PR #22  BGE-base-en embedding
PR #23  M13 v2 weights 학습
PR #24  step340 RAG (case memory)
PR #25  session_behavioral_drift
PR #26  aegis report --explain
PR #27  Phi-3.5 upgrade path + Metal
PR #28  audit log rotation
PR #29  aegis uninstall + --explain --json
```

dogfood: **14/14 PASS.** 회귀: **7/7 PASS.** 1426 tests.

운영 lifecycle 완료: install → use → debug (`--explain`) → audit
verify (rotation 인식) → uninstall (clean removal).

---

## 5. 다음 트랙 후보

| Track | 효과 |
|---|---|
| T2 sidecar (Docker daemon) | Phi-3.5 cold load 제거 |
| Shadow → M13 v3 retrain pipeline 검증 | patent value (한 달 후) |
| Hybrid M13 threshold calibration | mundane Bash false-positive 감소 |
