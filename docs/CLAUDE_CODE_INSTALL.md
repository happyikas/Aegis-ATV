# Claude Code 플러그인 설치 가이드 (T2 / Solo Free)

> 맥미니 또는 맥북에서 Aegis Plugin 을 실제 Claude Code 에 연결합니다.
> 한 번 설치하면 모든 Claude Code 세션의 모든 tool 호출이 자동으로
> Aegis firewall 을 통과합니다 (PreToolUse 차단 + PostToolUse 감사 +
> Stop cost 백필).

소요 시간: **5분.** 외부 API 키 / Anthropic 토큰 필요 없음.

---

## 1. 사전 준비

| 도구 | 버전 | 설치 |
|---|---|---|
| Claude Code | 1.0+ | App Store / `claude.com/claude-code` |
| `uv`        | ≥ 0.4 | `brew install uv` |
| Python      | 3.11+ | `uv sync` 가 자동 설치 |

```bash
# 저장소 클론 + 의존성 설치
git clone https://github.com/aegisdata/MVP.git aegis-mvp
cd aegis-mvp
uv sync                                  # → .venv/bin/python 생성
```

---

## 2. 설치

### 2.1 권장 (Solo Free)

```bash
uv run aegis install --mode local
```

세 개의 hook 이 `~/.claude/settings.json` 에 등록됩니다:

| Hook | 역할 |
|---|---|
| **PreToolUse** | 모든 tool 호출 직전에 firewall 통과 (BLOCK/ALLOW/REQUIRE_APPROVAL). |
| **PostToolUse** | tool 실행 결과를 ATMU 2PC phase 2 로 commit + 감사 chain 기록. |
| **Stop** | 세션 종료 시 transcript 의 token cost 백필. |

기본은 `--judge dummy` (키워드 매칭) — 가장 빠르고 false positive 가
거의 없습니다. 실제 코딩 AI 사용에 충분합니다.

### 2.2 Hybrid mode (실험적, P1 follow-up)

```bash
uv run aegis install --mode local --judge hybrid
```

heuristic + keyword + M13 attribution head 를 활성화. AWS 시크릿 키 +
loop attack 같은 시나리오를 잡지만, **현재 M13 threshold (0.40) 이
너무 낮아 실제 Bash 명령어 다수에 REQUIRE_APPROVAL 가 발생합니다.**
P1 follow-up 으로 calibration 진행 중. 사용 시 `AEGIS_APPROVE_AS_BLOCK=0`
환경변수를 같이 설정해서 워닝만 띄우도록 하세요:

```bash
# settings.json 의 PreToolUse command 앞에 AEGIS_APPROVE_AS_BLOCK=0 추가
```

### 2.3 재설치 / 업데이트

```bash
uv run aegis install --mode local --force
```

`--force` 는 기존 Aegis-owned 항목 (PreToolUse / PostToolUse / Stop)
을 모두 제거하고 새로 추가합니다 — 저장소 경로가 바뀌었거나, 모드를
바꿀 때 사용하세요.

### 2.4 제거

```bash
# settings.json 직접 편집
$EDITOR ~/.claude/settings.json
# "hooks" 섹션의 Aegis-owned 항목들 제거
```

자동 uninstall 명령은 P2 — 현재는 수동 편집 또는 백업파일 복원
(`~/.claude/settings.json.bak.<timestamp>`).

---

## 3. 설치 후 검증 (필수)

**Claude Code 를 재시작하기 전에** 반드시 다음을 실행하세요:

```bash
./scripts/dogfood_check.sh
```

8 개 체크가 통과해야 합니다:

```
[1] settings.json shape         ✓
[2] PreToolUse — innocuous ls   ✓ (ALLOW)
[3] PreToolUse — rm -rf /       ✓ (BLOCK)
[4] PostToolUse — accepts       ✓
[5] Stop — accepts              ✓
[6] Audit chain populated       ✓
[7] First-call latency          ✓ (<5000 ms)
[8] Install idempotency         ✓
```

`✓ 7/7 checks passed — green-light for real Claude Code` 가 나오면
Claude Code 재시작 후 사용 가능. **하나라도 ✗ 가 있으면 절대 재시작하지
마세요** — Claude Code 가 후크 호출 시 죽을 수 있습니다.

### 3.1 dogfood_check 가 잡는 실제 버그들

이 스크립트는 다음을 잡았습니다 (모두 PR #20 에서 수정):
- 시스템 `python3` 사용 → `numpy` 모듈 없음 → 후크 crash
- `--force` 재실행 시 hook 항목이 중복 누적
- 저장소 경로 변경 후 stale entry 가 settings.json 에 남음

---

## 4. 일상 사용

### 4.1 후크가 동작하는지 확인

새 Claude Code 세션을 시작하고 아무 명령이나 실행:

```
claude> Bash: ls
```

후크가 동작하면 `~/.aegis/audit.jsonl` 에 줄이 추가됩니다:

```bash
tail -f ~/.aegis/audit.jsonl
```

각 줄에는 timestamp, tool, decision (ALLOW/BLOCK/REQUIRE_APPROVAL),
latency 가 포함됩니다.

### 4.2 BLOCK 이 떨어졌을 때

후크가 명령을 거부하면 Claude Code 출력에 다음과 같이 표시됩니다:

```
PreToolUse:Bash hook error: [aegis-local] BLOCK Bash trace=...
  reason: dummy judge: matched keyword 'rm -rf /'
```

이는 정상 동작. 의도한 명령이라면 직접 터미널에서 실행하거나, 후크
일시 비활성화 (§4.4) 후 재시도.

### 4.3 감사 로그 검증

```bash
uv run aegis verify-audit
```

SHA3 chain 의 무결성을 검증. 누가 (또는 다른 도구가) 후크 외부에서
audit.jsonl 을 수정하면 여기서 잡힙니다.

### 4.4 후크 일시 비활성화

```bash
# settings.json 의 hooks 섹션을 잠시 비움
mv ~/.claude/settings.json ~/.claude/settings.json.disabled
# Claude Code 재시작 → 후크 없음
# 다시 활성화:
mv ~/.claude/settings.json.disabled ~/.claude/settings.json
```

---

## 5. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| Claude Code 재시작 후 `Hook execution failed` | dogfood_check 안 돌렸음. §3 실행 |
| `No module named 'numpy'` 에러 | 옛날 `python3` 후크 사용 — `--force` 로 재설치 |
| 모든 Bash 명령이 REQUIRE_APPROVAL | hybrid judge — `--judge dummy` 로 재설치 (§2.1) |
| `~/.aegis/audit.jsonl` 안 생김 | 후크가 실행 안 됨 — `dogfood_check.sh --hybrid` 로 진단 |
| `aegis install` 이 stale entry 들과 충돌 | `--force` 추가 |
| settings.json 손상 우려 | 매 install 마다 `settings.json.bak.<ts>` 자동 백업 — 복원 가능 |

---

## 6. 알려진 P1 이슈 (다음 PR 에서 처리 예정)

1. **Hybrid M13 threshold calibration** — 현재 0.40 은 mundane Bash 도
   REQUIRE_APPROVAL 시켜서 사용성 떨어짐. 0.55–0.65 로 조정 필요.
2. **`aegis uninstall` 명령** — 현재 수동.
3. **첫 콜 latency 100–500 ms** — Python 인터프리터 + numpy import.
   사용자 체감 거의 없으나 hot path 에서는 신경쓰임. PYC 캐시 + lazy
   import 로 50 ms 이하 가능.
4. **`~/.aegis/audit.jsonl` 회전 정책** — 무한 추가됨. logrotate 또는
   size-based 회전 추가 필요.

---

## 7. Aegis 가 잡는 사고 (검증된 시나리오)

`./scripts/macmini_user_test.sh --hybrid` 로 7개 시나리오 검증:

| # | 시나리오 | 잡는가 (`--judge dummy`) | 잡는가 (`--judge hybrid`) |
|---|---|:---:|:---:|
| 1 | Replit DB 삭제 | ✅ | ✅ |
| 2 | AWS 시크릿 push | ❌ | ✅ |
| 3 | CLAUDE.md drift | ✅ | ✅ |
| 4 | 동일 명령 5번 루프 | ❌ | ✅ |
| 5 | 1.4M 토큰 cost runaway | ✅ | ✅ |
| 6 | 멀티 에이전트 escalation | ✅ | ✅ |
| 7 | `/etc/passwd` 탐색 | ✅ | ✅ |

dummy 로도 5/7. hybrid 로 7/7 — 단, hybrid 의 false positive 문제
(P1 §6.1) 해결 전까지는 dummy 권장.

---

## 8. 다음 단계

- 하루 사용 후 `~/.aegis/audit.jsonl` 의 ALLOW vs BLOCK 비율 확인
- `uv run aegis report` 로 5-line risk summary
- `uv run aegis cost` 로 token spend 추적
- 문제 발생 시 `dogfood_check.sh` 재실행으로 진단
