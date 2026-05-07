# Aegis Personal — 5-minute quickstart

**대상**: macOS / Linux 에서 Claude Code 를 쓰는 개인 개발자.
**소요 시간**: 약 5분 (의존성 다운로드 포함).
**비용**: $0 (외부 API 호출 0).
**전제**: `claude` CLI 가 이미 깔려 있을 것.

---

## TL;DR — 4 줄

```bash
git clone https://github.com/happyikas/Aegis-ATV.git && cd Aegis-ATV
uv sync                              # 의존성 (~30s)
uv run aegis install --mode local    # ~/.claude/settings.json 패치
# Claude Code 재시작 — 끝.
```

이제 Claude Code 안에서 무엇을 하든 매 도구 호출이 Aegis 의 16-step firewall 을 거칩니다. 외부 API 호출 0건, 모든 처리 본인 디바이스에서 끝.

---

## 첫 BLOCK 보기 — 30 초

설치 후 Claude Code 를 새 터미널에서 켜고:

```
> "/var/data 디렉터리를 재귀적으로 삭제하는 명령" 을 실행해줘
```

기대 결과 (즉시):
```
⛔ BLOCK  Bash  trace=...
  reason: dangerous pattern: <step310 regex>
  advise:
    [HIGH] security-reviewer — Block until a human reviewer ACKs.
      • require-approval reason=destructive operation matched detection rule
```

도구는 실행되지 않습니다. Claude 에게는 "BLOCKED" 신호만 전달됨.

**다른 차단 시연** (모두 paraphrase 형태로 표기 — 실제 명령어로 시도):

| 시도 | 차단 룰 |
|------|---------|
| `force-push` 를 main / master / production 브랜치에 | `rule:git_destructive` |
| Kubernetes `delete` 명령 (namespace / deployment / pod) | `rule:cloud_destructive` |
| Terraform `destroy` 또는 `apply -auto-approve` | `rule:cloud_destructive` |
| AWS IAM `delete-policy` / `delete-user` | `rule:cloud_destructive` |
| AWS EC2 `terminate-instances` | `rule:cloud_destructive` |
| Helm `uninstall` 또는 `delete` | `rule:cloud_destructive` |
| AWS S3 bucket recursive remove with `--force` | `rule:cloud_destructive` |
| Read of cloud credentials path (`~/.aws/credentials`) | `rule:sensitive_path_block` |
| Read of SSH private key | `rule:sensitive_path_block` |
| SQL drop-table on a production table | `rule:sql_unbounded` |
| Unbounded SQL delete (no WHERE) | `rule:sql_unbounded` |
| Privileged install via elevated-privilege command | `rule:privilege_prefix` |

→ **31 종 룰이 활성**. 자세한 카탈로그: [`policies/rag_corpus/rules.jsonl`](../policies/rag_corpus/rules.jsonl).

---

## 무엇이 보호됩니까

매 PreToolUse 시점에 **16-step firewall** 이 발화:

| Step | 대상 | 예시 |
|------|------|------|
| step305 | safe-allowlist fast-path | `ls`, `git status`, `pytest` → <5ms |
| step309 | poisoned instruction drift | `CLAUDE.md` / `.mcp.json` 변조 감지 |
| step310 | dangerous-pattern args | filesystem purge, drop-table, system passwd path, privilege prefix, eval/exec |
| step311 | cloud destructive | k8s / Terraform / AWS IAM / EC2 / Helm / git force-push to main |
| step320 | blast radius | 파일/네트워크/cloud reach 평가 |
| step335 | cost gate | 토큰 예산 초과 감지 |
| step336 | loop detector | 같은 도구 + 같은 args 3회 → REQUIRE_APPROVAL |
| step340 | sLLM judge + RAG | 회색 케이스에 정책 + playbook 적용 |
| step360 | audit chain | Ed25519 sign + SHA3-256 Merkle |

---

## Solo Free 컨트랙트 — 데이터가 어디로 가나?

```
사용자 디바이스 안 (전부 로컬, 0 외부 호출):
  • Claude Code 가 PreToolUse hook 호출
  • aegis_local_hook.py 가 in-process 16-step firewall 실행
  • dummy provider — SHA3 임베딩 + 룰 기반 verdict
  • ~/.aegis/audit.jsonl — append-only signed log

밖으로 안 나가는 것:
  ❌ 도구 인자
  ❌ 파일 내용
  ❌ 명령어
  ❌ 추론 결과

옵션으로 *명시적* 으로 켤 때만:
  ✓ AEGIS_JUDGE_PROVIDER=haiku + ANTHROPIC_API_KEY → Anthropic 호출
  ✓ AEGIS_EMBEDDING_PROVIDER=openai + OPENAI_API_KEY → OpenAI 호출
  ✓ AEGIS_EMBEDDING_PROVIDER=bge-local → 한 번 GGUF 다운, 그 후 로컬
```

기본 install 으로는 *클라우드 호출 0*. `~/.aegis/audit.jsonl` 만 본인 디스크에 쌓임.

---

## 자주 쓰는 4 가지 명령

```bash
# 1) 차단 통계 5줄 요약
uv run aegis report
#   audit log: ~/.aegis/audit.jsonl  (4,347 entries)
#   ✅ 1,956 safe tool calls auto-approved
#   ⛔   23 destructive commands blocked
#   ⚠️    5 high-risk actions required approval
#   💸   12 redundant calls deduplicated
#   🔁    2 potential loops aborted

# 2) audit chain 위변조 검증
uv run aegis verify-audit
#   ✓ 4,347 records verified — chain intact from genesis

# 3) 정책 / 룰 변경 이력
uv run aegis policy diff --since 7d
#   added (2): rule-aws-rds-delete (2026-05-04), playbook-curl-pipe-bash
#   retired (1): rule-aws-iam-mutation-v0 → superseded by rule-aws-iam-mutation

# 4) 90 케이스 결정성 자가 검증 (CI / 이상 의심 시)
uv run python -m demo.macmini all
#   100 / 100 PASS
```

---

## 끄기 / 다시 켜기

```bash
uv run aegis uninstall              # ~/.claude/settings.json 에서 hooks 제거
                                     # audit log 는 보존 (~/.aegis/audit.jsonl)

uv run aegis install --mode local    # 다시 켜기
```

`~/.claude/settings.json` 의 backup 이 매 install 시 자동 생성 (`settings.json.bak.<timestamp>`).

---

## 모델 업그레이드 (선택)

기본 **dummy provider** 는 룰 기반이라 빠르지만 회색 케이스 분류가 약합니다. 본격 사용 시:

```bash
# 권고 — RAG 모드 (애매한 케이스에서 정확한 판단)
uv run aegis pull-model --model phi3-mini    # 2.2 GB GGUF
export AEGIS_JUDGE_MODEL_PATH=$PWD/models/Phi-3.5-mini-instruct-Q4_K_M.gguf
uv run aegis install --mode local --judge local-phi --force

# 더 빠른 모드 — RAG OFF, 1B 모델
uv run aegis pull-model --model llama-3.2-1b
export AEGIS_RAG_ENABLED=0

# Anthropic Haiku (클라우드, 가장 정확)
export ANTHROPIC_API_KEY=sk-ant-...
uv run aegis install --mode local --judge haiku --force
```

`uv run aegis pull-model --recommend` 로 항상 최신 권고 표 확인.

---

## 트러블슈팅

### Claude Code 가 hook 을 안 부른다

```bash
# 1) hook 이 등록되어 있는지
cat ~/.claude/settings.json | grep -A1 PreToolUse
# 2) 실행 권한 확인
ls -l tools/aegis_local_hook.py
# 3) Python venv 가 살아있는지
uv run python -c "import aegis"
# 4) Claude Code 를 *완전히 종료 후 재시작* 하셨나요? (탭 재열기로 부족)
```

### 매 호출이 너무 느림

```bash
# 1) sLLM 모델 사용 중인지
echo $AEGIS_JUDGE_PROVIDER
# 2) hybrid 또는 dummy 가 가장 빠름
uv run aegis install --mode local --judge dummy --force
```

### audit log 가 너무 큼

`~/.aegis/audit.jsonl` 은 append-only. 주기적 archive:

```bash
# 90일 이전 라인 별도 파일로
python3 -c "
import json, time
cutoff = time.time_ns() - 90 * 86400 * 10**9
with open('audit.jsonl') as f, open('audit.archive.jsonl', 'a') as out:
    for line in f:
        if json.loads(line).get('ts_ns', 0) < cutoff:
            out.write(line)
"
```

### 차단되면 안 되는 것까지 차단됨

```bash
# 1) 어떤 룰이 fired 했는지 확인
tail -1 ~/.aegis/audit.jsonl | python3 -m json.tool | grep reason
# 2) 해당 룰을 safe-allowlist 에 추가 (policies/safe_actions.json)
# 3) Claude Code 재시작
```

### 비상 복구 — `aegis install --rescue`

`aegis install` 을 잘못된 옵션으로 실행해서 hook 이 본인을 막는 경우 (cost gate · 누락된 환경변수 등):

```bash
# 별도 터미널에서 (Claude Code 안 거치는):
uv run aegis install --rescue
# → ~/.claude/settings.json.bak.<latest> 자동 복원
```

매 install 마다 자동 backup 이 생성되므로 마지막 working state 로 즉시 되돌릴 수 있습니다. PR #101 부터 Personal `--mode local` install 은 anti-self-DoS default 가 자동으로 적용돼 (`AEGIS_APPROVE_AS_BLOCK=0` + `AEGIS_TOKEN_BUDGET=99999999`) 이런 상황이 더 이상 발생하지 않습니다.

---

## 다음 단계

* **3-min 데모 영상** — [demo/recording/demo.gif](../demo/recording/demo.gif)
* **상세 매뉴얼 (한국어)** — [`docs/MANUAL_v2.2.md`](MANUAL_v2.2.md)
* **ATV 구조 시각자료** — [`docs/diagrams/atv_2080_v1.png`](diagrams/atv_2080_v1.png)
* **90-case 회귀 검증** — [`docs/MANUAL_macmini_validation.md`](MANUAL_macmini_validation.md)
* **GitHub Issues** — 버그 / 기능 요청 환영

---

## 한 줄 요약

> **"Claude Code 가 destructive 도구를 실행하기 전에, Aegis 가 그 호출을 본인 디바이스에서 cryptographically 검사 + 서명 + chain 합니다. 외부 API 호출 0, 의존성 docker 0, 첫 BLOCK 까지 5분."**
