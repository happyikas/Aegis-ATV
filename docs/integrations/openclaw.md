# Aegis × OpenClaw — 통합 분석 + 이슈 카탈로그

**작성**: 2026-05-08
**상태**: 분석 단계 (구현 미착수)
**대상**: [openclaw/openclaw](https://github.com/openclaw/openclaw) — "Personal AI assistant. Any OS. Any Platform." TypeScript / Node.js, Gateway 기반 multi-channel agent runtime.

---

## 1. 한 줄 요약

> **OpenClaw 는 Aegis 통합이 가장 깔끔한 환경**. `before_tool_call` 후크가 정확히 Aegis 의 `BLOCK / REQUIRE_APPROVAL / ALLOW` semantic 으로 매핑되며, plugin SDK 가 TypeScript 공식 contract 로 제공됩니다. **Paperclip 과 정반대** — plugin 형태로 정식 차단 가능.

---

## 2. 핵심 적합성

### OpenClaw 의 `before_tool_call` 후크 = Aegis 가 필요한 모든 것

OpenClaw plugin spec ([docs.openclaw.ai/plugins/hooks.md](https://docs.openclaw.ai/plugins/hooks.md)) 의 공식 인용:

```typescript
api.on(
  "before_tool_call",
  async (event) => {
    return {
      params?: Record<string, unknown>;        // tool args 수정 가능
      block?: boolean;                          // 차단 가능
      blockReason?: string;
      requireApproval?: {                       // 승인 요청 가능
        title: string;
        description: string;
        severity?: "info" | "warning" | "critical";
        timeoutMs?: number;
        timeoutBehavior?: "allow" | "deny";
        onResolution?: (decision) => void;
      };
    };
  },
  { priority: 50, timeoutMs: 30000 }
);
```

이 contract 는 Aegis 의 verdict (`{decision, reason, trace_id, advise}`) 와 1:1 매핑됩니다:

| Aegis verdict | OpenClaw return |
|---|---|
| `decision: "ALLOW"` | `{}` (no decision) |
| `decision: "BLOCK"`, `reason: "..."` | `{ block: true, blockReason: aegisReason }` |
| `decision: "REQUIRE_APPROVAL"`, `reason`, `advise` | `{ requireApproval: { title, description, severity, ... } }` |

→ **Plugin 한 개 (`@openclaw/plugin-aegis`) 가 Aegis 의 모든 16-step firewall 을 OpenClaw 안에 정식으로 가져옴.**

### Paperclip 과의 비교

| | Paperclip | OpenClaw |
|---|---|---|
| Plugin 이 tool call intercept 가능? | ❌ Forbidden by spec | ✅ `before_tool_call` 의 공식 용도 |
| Plugin 이 BLOCK 신호 보낼 수 있나? | ❌ | ✅ `block: true` |
| Approval 요청 가능? | ❌ Plugin 으론 못 함 | ✅ `requireApproval` 객체 |
| Param rewrite | ❌ | ✅ `params` field |
| Async handler | ✅ (executeTool RPC) | ✅ (Promise) |
| TypeScript SDK | ✅ | ✅ |

---

## 3. OpenClaw 의 자체 보안 기능 vs Aegis 가 채울 갭

OpenClaw 의 [THREAT-MODEL-ATLAS](https://docs.openclaw.ai/security/THREAT-MODEL-ATLAS.md) 가 솔직하게 인정한 갭들이 **정확히 Aegis 의 강점**입니다.

### OpenClaw 가 이미 보유

| 기능 | 위치 |
|------|------|
| Tailscale gateway 인증 + AllowFrom/AllowList | Gateway layer |
| Session isolation (`agent:channel:peer` key) | per-session boundary |
| Fetched URL 의 XML wrapping (indirect injection 완화) | content layer |
| SSRF protection (DNS pinning, internal IP block) | Gateway |
| Skill moderation (GitHub account age + regex FLAG_RULES) | Marketplace |
| Exec approvals (allowlist + ask mode) | Tool layer |

### OpenClaw threat model 자체가 인정한 미해결 갭 → Aegis 가 채움

| 미해결 갭 | OpenClaw 자평 인용 | Aegis 의 대응 |
|-----------|-------------------|---------------|
| 토큰 평문 저장 | "High - Tokens stored in plaintext" | step310 sensitive-path block (`~/.aws/credentials`, SSH key 등) |
| Prompt-injection 차단 | "Detection only, no blocking; sophisticated attacks bypass" | step309 instruction drift + step340 sLLM judge |
| Skill sandboxing | "Critical - No sandboxing, limited review" | step320 blast-radius + step330 role gating |
| 위험 패턴 regex 검출 | (없음) | step310 (filesystem-purge / 테이블 삭제 / 권한상승) + step311 (k8s/Terraform/AWS IAM) |
| 암호 서명 audit chain | "Audit logging for config changes" — 미래 권고 | step360 SHA3 chain + Ed25519 (즉시 가용) |
| 비용 / loop 차단 | (없음) | step335 cost gate + step336 loop detector |

→ **OpenClaw + Aegis = 완전한 defense-in-depth**. OpenClaw 가 channel / session / SSRF 를 처리, Aegis 가 tool call 별 cryptographic firewall 추가.

---

## 4. 통합 아키텍처 — `@openclaw/plugin-aegis`

### 구성

```
@openclaw/plugin-aegis (Node.js / TypeScript)
├── package.json
│   └── openclawPlugin: { worker, manifest, ui }
├── src/manifest.ts          # plugin 메타데이터 + capabilities
├── src/worker.ts            # before_tool_call 핸들러
├── src/aegis-client.ts      # Aegis sidecar HTTP client
└── src/ui/AegisInsightsTab.tsx  # OpenClaw UI slot 에 advisor 표시
```

### worker.ts 의 핵심 흐름

```typescript
import { evaluateAegis } from "./aegis-client";

export async function register(api: PluginApi) {
  api.on("before_tool_call", async (event) => {
    const verdict = await evaluateAegis({
      tool: event.tool.name,
      args: event.tool.params,
      sessionId: event.session.id,
      channel: event.session.channel,
      cwd: event.runtime.cwd,
    });

    if (verdict.decision === "ALLOW") return {};

    if (verdict.decision === "BLOCK") {
      return {
        block: true,
        blockReason: `Aegis ${verdict.reason}`,
      };
    }

    if (verdict.decision === "REQUIRE_APPROVAL") {
      return {
        requireApproval: {
          title: `Aegis: ${verdict.advise?.[0]?.advisor ?? "review needed"}`,
          description: verdict.reason,
          severity: verdict.severity ?? "warning",
          timeoutMs: 60000,
          timeoutBehavior: "deny",
          pluginId: "openclaw-plugin-aegis",
        },
      };
    }

    return {};
  }, { priority: 80 });   // 일반 hook 보다 높은 우선순위
}
```

### Aegis 측 변경 (없음)

Aegis sidecar mode 의 `localhost:8000/evaluate` HTTP 엔드포인트가 이미 존재. Plugin 이 그걸 호출 — Aegis 코드는 변경 없음.

### Local-mode (Solo Free) 와의 호환

OpenClaw 는 self-hosted Gateway 가 default 이므로 Aegis 의 Solo Free 컨트랙트와 잘 맞음. 사용자가:

```bash
# Aegis 측 — pro profile
uv run aegis install --mode local --profile pro

# OpenClaw 측 — plugin 설치
openclaw plugin install @openclaw/plugin-aegis
```

이렇게 두 단계만으로 통합. 외부 클라우드 호출 0.

---

## 5. 통합 이슈 10가지

### Issue 1: 언어 차이 (Python ↔ TypeScript)

**문제**: Aegis 는 Python, OpenClaw plugin 은 Node.js/TypeScript. 직접 import 불가.

**해결책**:
- (a) **Aegis sidecar HTTP** (권장) — `localhost:8000/evaluate` 호출. 이미 sidecar mode 존재. 추가 latency ~5-10 ms.
- (b) **subprocess spawn** — `aegis evaluate --json` CLI 호출. Latency ~50-100 ms (Python 시작비용). 권장 안 함.
- (c) **Aegis 의 로직을 TypeScript 로 포팅** — 16-step firewall 재구현 필요. 작업량 6+개월. 권장 안 함.

**권장**: (a). Aegis 가 이미 FastAPI sidecar 를 제공하므로 추가 작업 거의 없음.

### Issue 2: Tool 명명 / args schema 차이

**문제**: Aegis 의 step320 blast-radius / step305 safe-allowlist 는 tool 이름 기반.

| Claude Code | OpenClaw (추정) |
|---|---|
| `Bash` | `shell` 또는 `exec` |
| `Read` | `read_file` |
| `Write` | `write_file` |
| `Edit` | `edit_file` |
| `Grep` / `Glob` | `search` |
| `WebFetch` | `web_fetch` |
| `Task` | (subagent_*) |

**해결책**:
- `policies/safe_actions.json` 에 OpenClaw 의 read-only tool 들 추가
- `src/aegis/atv/blast_radius.py` 의 tool→blast 매핑에 OpenClaw tool 추가
- 또는 plugin 측에서 tool 명을 normalize 한 후 Aegis sidecar 호출 (`tool: "shell" → "Bash"` 매핑)

**권장**: Plugin 측 normalize 가 더 깔끔 — Aegis 코드 unchanged.

### Issue 3: Audit log 이중화

**문제**: OpenClaw 자체 logging 시스템 + Aegis `~/.aegis/audit.jsonl` 둘 다 운영.

**해결책**:
- 별도 유지 (각자 책임 영역 명확)
- Aegis audit record 의 메타데이터에 OpenClaw `session_id` 를 stamp → cross-reference 가능
- OpenClaw plugin UI slot 에서 Aegis audit 의 해당 session 을 surface

**권장**: 이중 유지. `aegis forensic <session_id>` 로 OpenClaw session 의 Aegis 측 timeline 조회.

### Issue 4: Multi-channel context (Discord / Telegram / WhatsApp / Signal)

**문제**: OpenClaw 는 multi-channel — agent 가 Telegram 메시지로 도구 호출 트리거. Aegis ATV header 에는 channel 개념 없음.

**해결책**:
- Plugin 이 Aegis sidecar 호출 시 `channel` 메타데이터를 함께 전송
- Aegis ATV builder 의 `prompt_structure` subfield 에 channel one-hot 인코딩 추가
- step340 sLLM judge 가 channel 정보를 읽어 위험도 평가 (untrusted channel 일 시 conservative)

**작업량**: ATV builder 수정 ~1주.

### Issue 5: OpenClaw 의 exec-approvals vs Aegis 의 REQUIRE_APPROVAL

**문제**: OpenClaw 는 자체 approval 시스템 (`/approve` 명령). Aegis 의 REQUIRE_APPROVAL 도 별도 approval 흐름. 두 개가 충돌 / 중복 가능.

**해결책**: Plugin 의 `before_tool_call` 핸들러가 Aegis 의 `REQUIRE_APPROVAL` verdict 를 OpenClaw 의 `requireApproval: {...}` 객체로 변환 → 사용자는 OpenClaw 의 단일 UI 에서만 승인. Aegis 의 advise 는 `description` 필드에 첨부.

**권장**: 정확히 spec 대로 매핑. 위 worker.ts 코드 참조.

### Issue 6: Skill manifest 와 step309 instruction drift baseline

**문제**: Aegis step309 는 `CLAUDE.md` / `AGENTS.md` / `.mcp.json` baseline 과 비교. OpenClaw 는 skill 시스템이 다름 (`SOUL.md` 등).

**해결책**:
- `AEGIS_INSTRUCTION_BASELINE_PATH` 환경변수로 OpenClaw 의 skill manifest 들을 추가 baseline 에 포함
- 또는 OpenClaw plugin 이 `before_install` 후크 + `agent_turn_prepare` 후크에서 skill drift 를 자체 검출하고 Aegis sidecar 에 보고

**작업량**: Aegis 측 baseline path 확장 ~3일.

### Issue 7: Threat model 중첩 검토

**문제**: OpenClaw 가 이미 SSRF / channel auth / session isolation 처리. Aegis 가 같은 영역을 다시 차단하면 false positive.

**중첩 가능 영역**:

| 영역 | OpenClaw | Aegis | 결정 |
|------|----------|-------|------|
| Network egress | DNS pinning + IP block | (해당 step 없음) | OpenClaw 가 처리 — 중복 없음 |
| Channel auth | Tailscale + AllowList | (해당 없음) | OpenClaw — 중복 없음 |
| Session isolation | session key | (multi-tenant 는 sidecar M14) | 분리 영역 |
| URL fetch wrapping | indirect injection 완화 | (해당 없음) | OpenClaw — 중복 없음 |
| Exec allowlist | OpenClaw 의 allowlist | step305 safe-allowlist | **중복 가능** — config 통합 필요 |

**해결책**: OpenClaw 의 exec-approval allowlist 와 Aegis `policies/safe_actions.json` 을 동기화. Plugin 설치 시 Aegis allowlist 를 OpenClaw 측에 export, 또는 plugin manifest 의 `instanceConfigSchema` 에 옵션화.

### Issue 8: 라이선스

**문제**: Aegis 는 Apache-2.0. OpenClaw 의 라이선스 확인 필요.

**확인 작업**: [github.com/openclaw/openclaw/LICENSE](https://github.com/openclaw/openclaw/blob/main/LICENSE) 및 plugin SDK 라이선스 확인.

**호환 매트릭스**:
- Aegis Apache-2.0 + OpenClaw Apache-2.0 → ✅ 호환
- Aegis Apache-2.0 + OpenClaw MIT → ✅ 호환
- Aegis Apache-2.0 + OpenClaw GPL → ⚠ 검토 필요 (Apache 코드를 GPL plugin 에 직접 link 시 라이선스 충돌 가능)
- Aegis Apache-2.0 + OpenClaw BSL/proprietary → ⚠ 검토 필요

### Issue 9: 성능 — agent loop 의 추가 latency

**문제**: OpenClaw 의 agent loop 가 매 turn 마다 다중 tool call. Aegis sidecar 호출 추가 시 누적 latency.

**측정 (Aegis 측 실측)**:

| Aegis profile | Median latency per call |
|---|---|
| free (dummy/dummy) | ~5 ms |
| pro (M13 + bge-local) | ~33 ms |
| pro + local-phi judge | ~180 ms |
| cloud (Haiku) | ~420 ms |

**OpenClaw heartbeat 주기**: 정확한 값 미확인 — 보통 수 초 단위.

**해결책**:
- 사용자가 profile 선택 (Solo Free 사용자: free, 본격: pro, 정확도 우선: cloud)
- Plugin manifest 의 `instanceConfigSchema` 에 profile 옵션 노출

### Issue 10: OpenClaw 의 빠른 release 속도

**문제**: OpenClaw 는 release 주기가 빠름 (TypeScript / pnpm 기반 active 개발). `before_tool_call` 의 schema 가 변경될 가능성.

**해결책**:
- Plugin manifest 의 `apiVersion` pinning
- OpenClaw plugin SDK 의 versioning 정책 확인 (semver / breaking change 정책)
- Aegis plugin 도 OpenClaw release 별로 compatibility matrix 유지

**예시**:

```typescript
// manifest.ts
export default {
  id: "@openclaw/plugin-aegis",
  apiVersion: 1,           // OpenClaw plugin SDK 버전
  aegisCompatibility: ">=0.1.0 <0.2.0",  // Aegis sidecar 호환 범위
  // ...
};
```

---

## 6. 통합 단계 (권장 순서)

| 순서 | 작업 | 작업량 | 가치 |
|------|------|--------|------|
| 1 | `@openclaw/plugin-aegis` skeleton — manifest + before_tool_call worker + sidecar HTTP | 1주 | OpenClaw 안에서 즉시 차단 작동 |
| 2 | Tool 명명 normalization (Issue 2) | 3일 | step305/step320 정확도 |
| 3 | Multi-channel ATV 인코딩 (Issue 4) | 1주 | step340 sLLM judge 가 channel 위험도 반영 |
| 4 | UI slot — AegisInsightsTab.tsx | 1주 | OpenClaw dashboard 안에서 audit / advise 시각화 |
| 5 | Skill baseline 통합 (Issue 6) | 3일 | step309 instruction drift 활성 |
| 6 | OpenClaw plugin marketplace 등재 | 별도 | 배포 |

**MVP plugin 작업량**: 약 **3-4주** — Aegis 측 코드 변경 거의 없이 plugin 만 작성.

---

## 7. 데모 시나리오 — "OpenClaw 사용자 보호"

OpenClaw 의 multi-channel scenario 활용:

### Setup

- 사용자가 Telegram 으로 OpenClaw agent 와 대화
- Agent 는 Claude Code 모드로 동작 (또는 자체 LLM provider)
- Aegis plugin 활성

### 5가지 시나리오

| # | 사용자가 보낸 메시지 (Telegram) | OpenClaw agent 의 의도 | Aegis 의 차단 |
|---|-------------------------------|----------------------|--------------|
| 1 | "내 서버 정리해줘" | shell 도구로 시스템 디렉터리 재귀 삭제 | step310 `rule:fs_purge` BLOCK |
| 2 | "내 AWS 사용량 확인" | AWS 자격증명 파일 read | step310 `rule:sensitive_path_block` BLOCK |
| 3 | "릴리스 정리" | git force-push to main | step311 `rule:git_destructive` BLOCK |
| 4 | "오래된 데이터 정리" | DB 테이블 제거 SQL | step311 `rule:sql_unbounded` BLOCK |
| 5 | "API 한 번만 더 시도" → 30회 반복 | retry loop | step336 loop detector → REQUIRE_APPROVAL |

각 케이스에서 OpenClaw 의 Telegram 메시지로 사용자가 직접:
- ⛔ "Aegis 가 차단했습니다 — reason: ..." 알림 받음
- ⚠️ "승인 필요" 요청 시 `/approve` 또는 거부 결정

### 종합 출력

```bash
# OpenClaw side
$ openclaw audit --session telegram-2026-05-08
session: telegram-user-AAA
  3 BLOCKs (Cases 1, 2, 4)
  1 REQUIRE_APPROVAL → denied (Case 5)
  1 ALLOW (Case 3 was after user override)

# Aegis side (동일 데이터, cryptographic chain)
$ aegis forensic telegram-user-AAA
forensic timeline — selector=telegram-user-AAA, 5 record(s)
  17:21:34  shell    ⛔ BLOCK   rule:fs_purge       (signed)
  17:22:01  read     ⛔ BLOCK   rule:sensitive_path (signed)
  17:23:15  shell    ⛔ BLOCK   rule:sql_unbounded  (signed)
  17:23:42  shell    ⚠️  RA     loop-breaker        (signed)
  17:24:10  shell    ✅ ALLOW   user-approved        (signed)

$ aegis verify-audit
✓ verify-audit (local chain) — N records intact
  signing pubkey: loaded — Ed25519 verified
```

→ **두 시스템 둘 다 같은 사실을 기록**. OpenClaw 는 사용자 친화적, Aegis 는 cryptographic forensic.

---

## 8. License & 라이선스 호환성

| | License |
|---|---|
| Aegis | Apache-2.0 |
| OpenClaw | **확인 필요** ([repo LICENSE 직접 확인](https://github.com/openclaw/openclaw/blob/main/LICENSE)) |

확인 작업 필요. Apache-2.0 / MIT / BSD-3 면 호환, GPL/AGPL/SSPL 면 검토.

---

## 9. Open questions

- **OpenClaw 의 plugin SDK versioning 정책** — `apiVersion` 의 semver 보장?
- **OpenClaw 의 self-hosted vs cloud 배포** — Aegis 의 Solo Free 컨트랙트가 OpenClaw cloud 호스팅 환경에서 의미가 있는가?
- **OpenClaw 의 자체 cost / loop detection** — 추가 확인 필요 (`/concepts/agent-loop.md` 에서 미발견)
- **Multi-language plugin** — Python plugin 도 지원? Node.js plugin 만이라면 sidecar HTTP 필수
- **Marketplace listing** — OpenClaw 의 ClawHub 에 등재하려면 별도 협의 필요?

---

## 10. 다음 단계 옵션

1. **2시간 검증** — OpenClaw self-host + Aegis sidecar (`uv run uvicorn aegis.main:app`) + 직접 `before_tool_call` 핸들러 등록한 minimal plugin → 위 5 시나리오 중 1-2개 reproduce
2. **Plugin MVP** — `@openclaw/plugin-aegis` skeleton 작성 (1주)
3. **Multi-channel ATV 인코딩** — Aegis ATV builder 확장 (1주)
4. **Demo 영상** — OpenClaw + Telegram + Aegis plugin 으로 5 시나리오 walk-through

---

## 11. 참고 자료

OpenClaw 측:

- [openclaw/openclaw GitHub](https://github.com/openclaw/openclaw)
- [docs.openclaw.ai](https://docs.openclaw.ai/) — full doc index
- [Plugin hooks spec](https://docs.openclaw.ai/plugins/hooks.md) — `before_tool_call` 등 30+ 후크
- [Plugin SDK overview](https://docs.openclaw.ai/plugins/sdk-overview.md)
- [Threat model](https://docs.openclaw.ai/security/THREAT-MODEL-ATLAS.md)
- [Agent loop](https://docs.openclaw.ai/concepts/agent-loop.md)
- [Exec approvals](https://docs.openclaw.ai/tools/exec-approvals.md)

Aegis 측:

- [docs/PERSONAL_QUICKSTART.md](../PERSONAL_QUICKSTART.md) — 5분 설치
- [docs/MANUAL_v0.1.0_ko.md](../MANUAL_v0.1.0_ko.md) — 한글판 매뉴얼 (PR #116)
- [docs/integrations/paperclip.md](paperclip.md) — Paperclip 통합 분석 (대조 사례)
- [docs/launch/blog_post.md](../launch/blog_post.md) — 아키텍처 long-form
