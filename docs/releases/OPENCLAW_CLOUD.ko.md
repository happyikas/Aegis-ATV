# Aegis ATV — OpenClaw + Cloud LLM API 트랙 (한국어 정본)

> **상태**: 🟡 **Preview** (analysis 완료, 플러그인 구현 진행 중)
> **타겟**: Claude Code 의 closed CLI 가 부족하고 OpenClaw 의 *유연성* 이 필요한 사용자
> **설치**: `aegis install --target openclaw-cloud` (현재 stub — Roadmap 참조)

---

## 1. 이 트랙이 누구를 위한 것인가

세 가지 조건 중 **하나라도** 해당되면 이 트랙이 맞습니다:

1. **다채널 입력** 이 필요 — Telegram bot, Discord bot, Slack 봇, Web UI 가 모두 같은 agent 를 호출
2. **다중 LLM provider 사용** 가능성 보존 — Claude / GPT / Gemini 사이를 자유롭게 전환
3. **OpenClaw 의 Skill 시스템** 을 쓰고 싶음 (마켓플레이스 기반 도구 등록)

cloud LLM 의 모델 capability 는 그대로 누리되, agent 프레임워크의 자유도와 vendor lock-in 에서 자유로움 — **OpenClaw 의 핵심 가치 명제** 그대로입니다.

---

## 2. Claude Code 트랙과의 차이 (정확히)

| 차원 | Claude Code | OpenClaw + Cloud LLM |
|------|-------------|----------------------|
| 입력 채널 | 터미널 한정 | Telegram / Discord / Slack / CLI / Web 다중 |
| LLM provider | Anthropic 고정 | Anthropic / OpenAI / Google / Mistral 등 다중 |
| Tool 정의 | 고정 카탈로그 | manifest 로 임의 등록 (ClawHub 마켓플레이스) |
| Aegis 후크 모드 | PreToolUse / PostToolUse / Stop / PreCompact / SessionStart / UserPromptSubmit (5 종) | `before_tool_call` 한 종, but ALLOW/BLOCK/REQUIRE_APPROVAL/PARAM-REWRITE 4 모드 |
| 데이터 흐름 | 사용자 ↔ 단일 cloud (Anthropic) | 사용자 ↔ OpenClaw → cloud LLM 선택 |
| 비용 모델 | per-token (Anthropic 청구서) | per-token (다중 청구서, provider 마다) |

→ **Aegis 활용도는 Claude Code 트랙과 비슷하지만**, 다채널 attribution / param mutation / provider drift 감지 영역이 추가됩니다.

---

## 3. 권장 환경

OpenClaw 자체는 Node.js / TypeScript runtime. cloud LLM 호출이라 GPU 불필요.

```
┌────────────────────────────────────────────────────────┐
│  Telegram / Discord / Slack / CLI 사용자               │
└────────────────────┬───────────────────────────────────┘
                     ▼
┌────────────────────────────────────────────────────────┐
│  OpenClaw agent (Node.js / TypeScript)                 │
│  + @happyikas/openclaw-plugin-aegis                    │
└────────────────────┬───────────────────────────────────┘
                     ▼ before_tool_call
┌────────────────────────────────────────────────────────┐
│  Aegis sidecar (FastAPI, localhost:8000)               │
│  - 16-step firewall                                    │
│  - SHA3 + Ed25519 audit chain                          │
│  - per-channel attribution                             │
│  - per-provider drift detection                        │
└────────────────────┬───────────────────────────────────┘
                     ▼
┌─────────┐ ┌──────────┐ ┌────────┐ ┌─────────┐
│ Anthropic│ │  OpenAI  │ │ Google │ │ Mistral │
│  Claude │ │  GPT-4   │ │ Gemini │ │  Large  │
└─────────┘ └──────────┘ └────────┘ └─────────┘
```

---

## 4. 무엇이 작동할 예정인가

### 4-1. Claude Code 트랙과 동일하게 (즉시)

- 16-step firewall + audit chain
- 8-advisor pipeline
- Coach / Live / Doctor 매뉴얼의 모든 명령

### 4-2. 이 트랙 *고유* 기능

| 기능 | 어디서 옴 |
|------|----------|
| 다채널 attribution | OpenClaw event 의 channel 메타가 audit record 에 박힘 |
| Cross-channel injection 감지 | 한 채널 입력이 다른 채널 컨텍스트를 갈아끼우려 시도 시 BLOCK |
| Param rewrite (자동 redaction) | OpenClaw `before_tool_call` return `params` 로 secret placeholder 치환 후 통과 |
| Provider drift 감지 | 같은 prompt 가 provider 별 verdict 다름 → vendor change forensic |
| Provider switching cost-benefit 분석 | Coach 의 cost replay 가 provider 간 시뮬레이션 |
| Skill manifest baseline | step309 의 OpenClaw 변형 — 설치된 skill 의 SHA3 hash 추적 |
| ClawHub 마켓 typosquatting 감지 | skill 이름 유사도 매칭 (Snyk / Socket.dev 류) |
| `requireApproval` 자동 timeout 추천 | 학습된 historical pattern 으로 적정 timeout 자동 산정 |

### 4-3. 이 트랙에서 *못 보는 것* (정직하게)

cloud LLM 트랙이라서 발생하는 한계 — Local OSS 트랙으로 가야 풀림:

- 모델 가중치 hash baseline ❌
- 양자화 drift 감지 ❌
- Logit-level forensic ❌
- Server-side KV cache hit/miss ❌
- GPU 메트릭 ❌

---

## 5. Roadmap

| 단계 | 상태 | 설명 |
|------|------|------|
| 1. OpenClaw 분석 + 통합 가능성 검증 | ✅ 완료 | [PR #118](https://github.com/happyikas/Aegis-ATV/pull/118) |
| 2. `aegis install --target openclaw-cloud` stub | ✅ 완료 | [PR #127](https://github.com/happyikas/Aegis-ATV/pull/127) |
| 3. `@openclaw/plugin-aegis` TypeScript 스켈레톤 | ✅ 완료 | `openclaw-plugin/` — handler + HTTP client + 19 tests |
| 4. End-to-end OpenClaw runtime 통합 테스트 | 🟡 진행 중 | sidecar `/evaluate` ↔ 플러그인 |
| 5. 다채널 attribution 통합 | 🟡 설계만 | OpenClaw event metadata → ATV `header.channel` |
| 6. Param rewrite 통합 | ✅ 핸들러에 구현됨 | `sanitized_input` 처리 |
| 7. Provider drift 감지 | 🔴 예정 | Coach 의 새 학습 차원 |
| 8. npm publish | ✅ 완료 | [`@happyikas/openclaw-plugin-aegis@0.3.0`](https://www.npmjs.com/package/@happyikas/openclaw-plugin-aegis) on npm — GA, `latest` dist-tag. ClawHub 마켓 등록은 별도 후속 ([#150](https://github.com/happyikas/Aegis-ATV/issues/150) — upstream paused) |

→ 단계 3 (TypeScript 플러그인) 이 Local 트랙과 공유되므로, 그 PR 이 머지되면 **Cloud 트랙 4–5 는 빠르게 따라옴**.

---

## 6. 지금 할 수 있는 것 (Preview 사용자)

이 트랙이 GA 가 될 때까지 **Claude Code 트랙으로 시작** 권장 — 같은 firewall + audit chain. OpenClaw 자체를 익혀두고 싶으면:

```bash
# 1) OpenClaw 자체는 별도 설치
npx create-openclaw my-agent
cd my-agent
npm install

# 2) 일단 Aegis 는 Claude Code 트랙에 남겨두고
aegis install --target claude-code --mode local

# 3) OpenClaw + Cloud 가 GA 되면 (2026 H1 예정):
aegis install --target openclaw-cloud
```

---

## 7. 자주 묻는 질문

**Q. Claude Code 트랙과 OpenClaw + Cloud 트랙을 동시에 쓰면?**
A. 가능합니다. 두 트랙은 같은 `~/.aegis/audit.jsonl` 을 공유하지 않고 각자의 audit log 를 가집니다 — `aegis report --target claude-code` / `aegis report --target openclaw-cloud` 로 따로 조회.

**Q. provider 를 자주 바꾸는 게 보안 리스크인가?**
A. provider 마다 RLHF / safety tuning 이 다르므로 **같은 prompt 가 한 provider 는 거부, 다른 provider 는 허락** 할 수 있음 — Coach 가 학습해서 provider drift 알림으로 surfacing.

**Q. 비용 추적은 provider 별로 분리되나?**
A. 네. `aegis cost summary --by provider` (예정) 로 Anthropic / OpenAI / Google 청구서 시뮬레이션 분리 가능. 실제 청구서는 각 provider 의 dashboard 에서 확인.

**Q. ClawHub skill 의 보안은?**
A. NPM/PyPI supply chain 과 같은 위험. Aegis 가 step309 (manifest hash baseline) + 마켓 reputation 학습 (Coach §4-2) 으로 typosquatting / 악성 skill 감지.

---

## 8. 다른 트랙

- [📋 릴리스 인덱스](README.md) — 세 트랙을 1 페이지로 비교
- [Claude Code](CLAUDE_CODE.ko.md) — 가장 간단한 시작점 (GA)
- [OpenClaw + Local OSS LLM](OPENCLAW_LOCAL.ko.md) — air-gapped, 가장 깊은 instrumentation (Preview)
