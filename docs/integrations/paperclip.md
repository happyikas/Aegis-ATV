# Aegis × Paperclip — 통합 분석 + 데모 시나리오

**작성**: 2026-05-08
**상태**: 분석 단계 (구현 미착수)
**대상 외부 프로젝트**: [paperclip-ai/paperclip](https://github.com/paperclipai/paperclip) — "AI 노동력의 human control plane", MIT 라이선스, ~63K stars (2026-05 기준)

---

## 1. 요약

Paperclip 은 Claude Code / Codex / OpenClaw / 커스텀 스크립트 등 여러 AI agent 를 **회사 조직처럼 운영** 하는 control plane 입니다. 목표 / 조직도 / 티켓 / heartbeat / 예산 / 승인 / 감사 로그를 plugin + adapter 시스템으로 관리합니다.

Aegis 는 매 도구 호출을 **PreToolUse 시점에서 cryptographic firewall 로 차단** 하는 in-process 레이어입니다. 두 시스템은 동일한 도메인 (cost / performance / security) 을 다른 추상도에서 다루며 — Paperclip = 조직 단위 governance, Aegis = 호출 단위 prevention — 서로 보완적입니다.

> **핵심 발견**: **Paperclip plugin 만으로는 Aegis 의 BLOCK 가치 실현 불가.** Plugin spec 의 *Forbidden capabilities* 에 명시: "approval decisions, budget override, auth bypass, checkout lock override, direct DB access". 즉 plugin 은 agent tool call 을 intercept 또는 block 할 수 없습니다.

해결책: **adapter 레벨 통합이 핵심**. Paperclip 의 `claude_local` adapter 가 `claude` CLI 를 spawn 하면, Claude CLI 가 `~/.claude/settings.json` 의 PreToolUse hook 을 발화 → Aegis 가 자동으로 firewall 적용. **Zero-config 으로 Claude Code agent 들은 즉시 보호됨.**

---

## 2. 두 시스템의 architectural mapping

### 2.1 동일 도메인, 다른 layer

| Paperclip 의 layer | Aegis 의 대응 | 협력 방식 |
|---|---|---|
| Org chart / role / chain of command | step330 role-based gating | role 정보를 ATV 에 주입하여 step330 의 verb 권한 검사에 활용 |
| Ticket / approval workflow | step350 approval gate | Aegis REQUIRE_APPROVAL 결정이 Paperclip ticket 으로 escalate |
| Per-agent budget / cost tracking | step335 cost gate, M12 cost ledger | Paperclip budget 의 finer-grain enforcement |
| Append-only audit log | step360 audit chain (SHA3 + Ed25519) | Aegis chain 이 cryptographic 보강 |
| Heartbeat / loop detection | step336 loop detector | Aegis 의 "같은 호출 3회 → REQUIRE_APPROVAL" 이 Paperclip heartbeat의 사전 게이트 |
| Plugin / adapter 시스템 | sidecar `/evaluate` API + advisor surface | Aegis 가 Paperclip 의 plugin 으로 등록 가능 (read-only / advisor) |

### 2.2 8-advisor 카탈로그 매핑

Paperclip 의 운영 관심사 ↔ Aegis 의 advisor 카탈로그가 거의 1:1 매칭됩니다:

| Paperclip 운영 관심사 | Aegis advisor (8개) |
|---|---|
| budget / cost surge | `cost-optimizer`, `kv-cache-optimizer`, `context-compactor` |
| stalled work / heartbeat / loop | `loop-breaker`, `human-clarifier`, `test-runner` |
| risky tool call / privilege | `security-reviewer`, `permission-escalator` |

→ **Aegis 의 advisor recommendation 이 Paperclip 의 ticket 으로 자연스럽게 escalate 가능.**

---

## 3. 세 통합 layer

### Layer A: Adapter (preventive, 핵심)

**무엇**: Paperclip 의 `claude_local` adapter 가 `claude` CLI 를 child process 로 spawn 할 때, Claude CLI 가 `~/.claude/settings.json` 의 PreToolUse hook 을 발화. Aegis 가 그 hook 에 등록되어 있으므로 자동 작동.

**작업량**: 0 (Aegis + Paperclip 둘 다 self-host 한 사용자에겐 이미 작동)

**커버리지**:
- ✅ Claude Code adapter 가 spawn 하는 Claude session 의 모든 tool call
- ❌ 비-Claude adapter (Codex CLI, custom script, HTTP webhook) — Claude Code 의 hook 시스템 외부

**비-Claude adapter 확장** (작업 1-2주):
- Aegis 에 sidecar mode `/evaluate` HTTP endpoint 가 이미 존재 (`localhost:8000`)
- Paperclip adapter 의 `runChildProcess` 직전에 Aegis sidecar 호출:

```ts
// 가상 코드 — Aegis-aware adapter wrapper
import { evaluateAegis } from "@aegis-atv/paperclip-adapter";

export async function execute(ctx, config) {
  // ... existing config building ...

  // 새 firewall 단계
  const aegisVerdict = await evaluateAegis({
    tool: config.command,
    args: config.args,
    aid: ctx.agentId,
    cwd: config.cwd,
  });
  if (aegisVerdict.decision === "BLOCK") {
    throw new AdapterError(`Aegis BLOCK: ${aegisVerdict.reason}`);
  }
  if (aegisVerdict.decision === "REQUIRE_APPROVAL") {
    return { status: "needs_approval", aegisTraceId: aegisVerdict.trace_id };
  }

  return runChildProcess(/* ... */);
}
```

이로써 **모든 Paperclip-spawned agent** 가 Aegis 통과 — Claude / Codex / 커스텀 스크립트 / webhook 무관.

### Layer B: Plugin (observability + advisor surface)

**무엇**: `@paperclipai/plugin-aegis` Node.js 패키지로 Paperclip dashboard 안에 Aegis insight 시각화.

**Plugin 이 할 수 있는 것** (Paperclip plugin spec 기준):
- ✅ `events.subscribe` — `agent.run.started`, `agent.run.finished`, `budget.incident.opened`, `approval.created` 구독
- ✅ `data.register` — Paperclip UI slot 에 데이터 공급 (예: `aegis-insights-tab`)
- ✅ Custom plugin events 발행 — `plugin.aegis.attempt_blocked` 등
- ✅ `secret-ref` 형 설정 — Aegis 의 audit log 경로 / signing pubkey

**Plugin 이 할 수 없는 것**:
- ❌ Tool call intercept / block (forbidden by spec)
- ❌ Approval 결정 변경
- ❌ Budget override

**Plugin 작동 흐름**:

```
agent.run.started 이벤트
  → plugin worker 가 Aegis CLI 호출 (aegis report --json --since 1h)
  → Paperclip ticket 메타데이터에 Aegis insight 첨부
  → UI slot "aegis-insights-tab" 에 표시

approval.created 이벤트 (Aegis REQUIRE_APPROVAL → Paperclip approval 로 escalate)
  → plugin 이 aegis forensic <trace_id> --json 으로 컨텍스트 채움
  → 승인자가 Paperclip 안에서 Aegis 의 step trace + advisor 권고 보고 결정

budget.incident.opened 이벤트
  → plugin 이 aegis advise --category cost --json 호출
  → cost-optimizer / kv-cache-optimizer 권고를 Paperclip ticket comment 로 추가
```

**작업량**: 2-3주 — plugin manifest + worker + UI slot React 컴포넌트

### Layer C: Multi-tenant Sidecar (enterprise)

**무엇**: Paperclip 의 multi-company 운영 → Aegis sidecar mode (M14 AID quarantine + M15 encrypted journal + M16 HAM).

**매핑**:
- Paperclip company → Aegis tenant
- Paperclip agent_id → Aegis aid
- Paperclip approval workflow → Aegis step350 approval queue

**작업량**: 별도 trade-off 분석 필요 — Paperclip 의 cloud / hosted 단계가 명확해진 후 검토.

---

## 4. 권장 통합 순서

| 순서 | Layer | 작업량 | 가치 |
|------|-------|--------|------|
| 1 | A (zero-config Claude Code) | 0 | 즉시 — Paperclip 의 모든 Claude Code agent 보호 |
| 2 | A+ (비-Claude adapter wrapper) | 1-2주 | 모든 agent runtime 으로 확장 |
| 3 | B (plugin observability) | 2-3주 | Paperclip dashboard 에 Aegis 가시성 |
| 4 | C (multi-tenant sidecar) | 4+주 | enterprise 단계 |

---

## 5. 데모 시나리오 — "DemoCorp Inc"

10분 walk-through. 사용자가 Paperclip + Aegis 를 모두 self-host 한 상태 가정.

### 5.1 셋업

**Paperclip company**: "DemoCorp Inc"
**목표**: "DB 스키마 마이그레이션 + 블로그 공지"
**Agents**:

| 역할 | Agent runtime | Paperclip role |
|------|---------------|----------------|
| **PlannerBot** | Codex CLI | CEO — 큰 그림 분배 |
| **EngineerBot** | Claude Code | Engineer — 실제 코드 / 마이그레이션 작성 |
| **MarketingBot** | custom Python script (webhook adapter) | Marketing — 블로그 포스트 생성 |

**Aegis 셋업**:

```bash
# Aegis pro profile — 모든 advisor + bge-local + hybrid judge
uv run aegis install --mode local --profile pro
uv run aegis audit-key init        # 서명 활성화
```

**Paperclip 셋업**:

```bash
# Paperclip self-host
git clone https://github.com/paperclipai/paperclip
cd paperclip && pnpm install && pnpm dev
# localhost:3100 에서 dashboard 열림

# DemoCorp Inc 회사 생성 + 3 agent 등록
npx paperclipai onboard --yes
npx paperclipai company:create "DemoCorp Inc"
npx paperclipai agent:add EngineerBot --adapter claude_local
npx paperclipai agent:add PlannerBot --adapter codex_local
npx paperclipai agent:add MarketingBot --adapter custom-webhook
```

### 5.2 5가지 테스트 케이스

각 케이스마다 **Without Aegis** (가상 / 비교용) vs **With Aegis** (실제 시연) 동시 표시.

#### Case 1: 권한 상승 시도

**시나리오**: EngineerBot 이 미설치 패키지 빌드 위해 권한 상승 명령 실행 시도.

```
EngineerBot 의 의도: "DB driver 가 없네. 권한상승해서 패키지 매니저로 설치하자."
```

| | Without Aegis | With Aegis |
|---|---|---|
| Paperclip view | ✅ Run completed: 패키지 설치 성공 | ⛔ Run aborted: Aegis BLOCK 신호 |
| 시스템 영향 | system package 설치됨 (예상치 못한 system 변경) | 없음 |
| Aegis audit | — | step310 `rule:privilege_prefix` BLOCK, signed |
| Paperclip dashboard | "✅ Engineer task done" | "⚠ Engineer task: aegis blocked tool call" — 클릭하면 Aegis trace |

```
$ aegis report --since 1m
⛔ 1 destructive command blocked
   reason: dangerous pattern: privilege escalation prefix
```

#### Case 2: AWS 자격증명 read

**시나리오**: EngineerBot 이 "production DB 디버깅 위해 자격증명 확인하겠다" 며 AWS credential 파일 읽기 시도.

| | Without Aegis | With Aegis |
|---|---|---|
| Paperclip view | ✅ Read tool call success | ⛔ Aegis sensitive-path BLOCK |
| 보안 영향 | 자격증명이 LLM context 진입 → downstream leak 위험 (블로그 / 커밋 메시지 등으로 우연히 유출) | 자격증명이 LLM context 에 절대 진입하지 않음 |
| Advisor | — | `permission-escalator` 권고 → Paperclip approval ticket 자동 생성 |

```
$ aegis advise --category security
🔴 HIGH [SECURITY] permission-escalator ×1
   "Surface verdict to operator before proceeding."
   Why: sensitive-path read attempt on cloud credentials
```

#### Case 3: main 브랜치 force-push

**시나리오**: EngineerBot 이 마이그레이션 후 history 정리하려 force-push 시도.

| | Without Aegis | With Aegis |
|---|---|---|
| Paperclip view | ✅ Git push 성공 | ⛔ Aegis git_destructive BLOCK |
| 데이터 영향 | 다른 개발자의 commit 손실 가능 | 없음 |
| Aegis audit | — | step311 `rule:git_destructive` BLOCK |

#### Case 4: 비-Claude agent 의 production DB 테이블 삭제

**시나리오**: MarketingBot (custom Python script) 이 "old data 정리" 위해 production 테이블 제거 SQL 시도. **이 케이스가 Layer A+ (adapter wrapper) 의 가치 — Claude Code 가 아니라 custom adapter 가 spawn 한 agent.**

| | Without Aegis (또는 zero-config Aegis) | With Aegis Layer A+ |
|---|---|---|
| Paperclip view | ✅ Marketing task completed | ⛔ Aegis sql_unbounded BLOCK (adapter wrapper 가 sidecar `/evaluate` 호출) |
| 데이터 영향 | production table 삭제 — 복구 불가 | 없음 |
| 차이점 | Claude Code hook 없으니 zero-config 으로는 막을 수 없음 | adapter wrapper 가 모든 agent 로 보호 확장 |

이 케이스는 **Layer A+ 통합의 ROI 정량화**: 비-Claude agent 가 절반이라면 zero-config 만으로는 50% 만 보호.

#### Case 5: Cost runaway (loop)

**시나리오**: PlannerBot 이 retry loop 에 빠져 같은 task 30회 반복.

| | Without Aegis | With Aegis |
|---|---|---|
| Paperclip budget 시점 | 예산 초과 후에야 stop (이미 30회 호출) | step336 가 3회에 REQUIRE_APPROVAL 격상 |
| 비용 | $X (30회 × per-call) | $X/10 (3회까지만 + 인간 승인) |
| 권고 | — | `loop-breaker` advisor → Paperclip ticket comment "이 task 는 무한루프 가능성" |

### 5.3 시연 후 종합 출력

5분 시연 후 다음 명령어 차례로 실행:

```bash
$ uv run aegis report --since 10m
audit log: ~/.aegis/audit.jsonl  (47 entries)
  ✅  3 safe ALLOW
  ⛔  4 destructive BLOCKs (Cases 1, 2, 3, 4)
  ⚠️  1 REQUIRE_APPROVAL (Case 5 loop)
  🔁  1 loop aborted

$ uv run aegis verify-audit
✓ verify-audit (local chain) — 47 records intact
  signing pubkey: loaded — Ed25519 verified

$ uv run aegis advise --since 10m
aegis advise — 3 recommendation(s) from last 10m
  🔴 HIGH [SECURITY] permission-escalator ×3
  🔴 HIGH [COST]     loop-breaker ×1
  🟡 MEDIUM [SECURITY] security-reviewer ×1

$ uv run aegis forensic last
forensic timeline — selector=democorp-run-A, 5 record(s)
  17:21:34  EngineerBot   Bash    ⛔ BLOCK  (165 ms)
              └─ reason: rule:privilege_prefix
  17:21:42  EngineerBot   Read    ⛔ BLOCK  (180 ms)
              └─ reason: rule:sensitive_path_block
              └─ advisor: invoked (permission-escalator)
  17:21:49  EngineerBot   Bash    ⛔ BLOCK  (210 ms)
              └─ reason: rule:git_destructive
  17:21:56  MarketingBot  Bash    ⛔ BLOCK  (95 ms via sidecar)
              └─ reason: rule:sql_unbounded
  17:22:03  PlannerBot    Bash    ⚠️ RA      (118 ms)
              └─ reason: step336 loop detected (3rd identical call)
              └─ advisor: invoked (loop-breaker)
```

### 5.4 시연 핵심 포인트

| 메시지 | 시연 근거 |
|--------|-----------|
| "Paperclip 의 빠른 agent 운영 + Aegis 의 호출 단위 prevention 이 보완" | 5가지 케이스 모두 Paperclip 이 task 추적 + Aegis 가 사전 차단 |
| "Aegis 의 audit chain 이 Paperclip audit 위에 cryptographic 보강 추가" | `verify-audit` 출력 — Paperclip 의 일반 log 와 달리 SHA3 + Ed25519 |
| "비-Claude agent 까지 일관된 firewall — Layer A+ 의 ROI" | Case 4 — adapter wrapper 만이 막을 수 있음 |
| "Advisor 권고가 Paperclip ticket 으로 escalate" | `aegis advise` 출력 — Plugin Layer B 가 이걸 dashboard 에 surface |
| "Forensic timeline 으로 사후 분석 가능" | `aegis forensic` 출력 — 단일 Paperclip run 안의 5 시도 추적 |

### 5.5 시각 자료 (선택)

- "Without Aegis" / "With Aegis" 시계열 비교 다이어그램
- Paperclip dashboard mock — Aegis insights tab 위치
- ATV-2080-v1 벡터가 Paperclip 의 ticket metadata 에 어떻게 매핑되는지

---

## 6. License & 라이선스 호환성

| 시스템 | 라이선스 |
|--------|----------|
| Paperclip | MIT |
| Aegis | Apache-2.0 |

**호환성**: 양립 가능. Apache-2.0 의 NOTICE 보존 의무는 Aegis 측 코드에만 적용. Paperclip plugin / adapter 형태로 배포 시 별도 라이선스 침해 위험 없음.

특허 grant: Apache-2.0 §3 의 명시적 특허 grant 가 Paperclip 사용자에게 자동 전이 — Aegis 의 AegisData patent v4 청구항이 cover 됨.

---

## 7. Open questions

- **Paperclip Labs 와 사전 협의** 가 필요한가? Plugin / adapter 를 별도 npm 패키지로 publish 하는 한 협의 불필요. Paperclip 의 공식 plugin marketplace 에 등재하려면 협의 필요할 수 있음.
- **Aegis 의 `aegis advise --json` 출력 schema 안정성** — Plugin Layer B 가 의존하므로 v0.1.x 동안 schema 변경 최소화 필요.
- **Paperclip plugin SDK 의 versioning** — `apiVersion: 1` 이 v0.1.x 동안 유지될지 불확실 (Paperclip 의 빠른 release 속도 고려).
- **비-Claude adapter 의 sidecar HTTP overhead** — Layer A+ 의 매 호출당 +5-10ms latency 추가. Paperclip 의 heartbeat 주기와 trade-off 평가 필요.

---

## 8. 다음 단계 옵션

1. **30분 검증** — Paperclip + Aegis (`--profile pro`) 둘 다 self-host, EngineerBot 으로 Claude Code agent 등록, 의도적으로 Case 1-3 시도. Layer A 자동 작동 확인.
2. **Layer A+ POC** — 비-Claude adapter wrapper 의 prototype 작성 (Python script + HTTP sidecar). Case 4 시연 가능.
3. **Layer B prototype** — `@paperclipai/plugin-aegis` 의 manifest + 기본 worker (`aegis report --json` polling) 작성. UI slot 은 후속.
4. **데모 영상** — 위 5 케이스를 실제로 reproducing 하는 ~10분 walk-through 영상 (Show HN / 블로그 자료).

---

## 9. 참고 자료

- Paperclip 공식: [paperclip.ing](https://paperclip.ing) · [docs.paperclip.ing](https://docs.paperclip.ing)
- Plugin spec: [github.com/paperclipai/paperclip/blob/master/doc/plugins/PLUGIN_SPEC.md](https://github.com/paperclipai/paperclip/blob/master/doc/plugins/PLUGIN_SPEC.md)
- Adapter docs: [docs.paperclip.ing/adapters/creating-an-adapter](https://docs.paperclip.ing/adapters/creating-an-adapter)
- Local CLI adapters (deepwiki): [deepwiki.com/paperclipai/paperclip/5.2-local-cli-adapters](https://deepwiki.com/paperclipai/paperclip/5.2-local-cli-adapters)

Aegis 측:

- [docs/PERSONAL_QUICKSTART.md](../PERSONAL_QUICKSTART.md) — 5분 설치
- [docs/MANUAL_v0.1.0_ko.md](../MANUAL_v0.1.0_ko.md) — 한글판 매뉴얼 (PR #116)
- [docs/launch/blog_post.md](../launch/blog_post.md) — 아키텍처 long-form
