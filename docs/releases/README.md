# Aegis ATV 개인용 — 3개 릴리스 트랙

Aegis ATV 의 개인용 (Personal MVP) 버전은 사용자의 환경에 따라 **3개 릴리스 트랙**으로 분리됩니다. 같은 firewall + audit chain 코어 위에, 통합 대상 (Claude Code / OpenClaw) 과 LLM 위치 (cloud / local) 의 조합으로 결정됩니다.

## 1초 결정 매트릭스

```
"매일 Claude Code 쓰고, 빨리 시작하고 싶다"
  → 🟢 Claude Code 트랙 (GA)

"Telegram 봇으로 팀 단위 agent 운영하고 싶다, provider 자유롭게"
  → 🟡 OpenClaw + Cloud LLM 트랙 (Preview)

"데이터를 외부에 절대 못 보낸다, 자체 호스팅 LLM 필요"
  → 🟡 OpenClaw + Local OSS LLM 트랙 (Preview)
```

## 3개 트랙 한눈에

| | 🟢 Claude Code | 🟡 OpenClaw + Cloud LLM | 🟡 OpenClaw + Local OSS |
|---|----------------|------------------------|--------------------------|
| **상태** | **GA** (v0.1.0) | Preview | Preview |
| **설치** | `aegis install --target claude-code` | `aegis install --target openclaw-cloud` | `aegis install --target openclaw-local` |
| **LLM 위치** | Anthropic cloud | Anthropic / OpenAI / Google cloud | 사내 / 로컬 (Llama / Qwen / Mistral) |
| **외부 호출** | Claude Code 가 Anthropic 호출 (Aegis 자체 0) | OpenClaw 가 cloud LLM 호출 | **0** (air-gapped 가능) |
| **하드웨어** | 노트북 OK | 노트북 OK | GPU 권장 (모델 크기에 따라) |
| **데이터 residency** | provider 측 | provider 측 | 100% 사내 |
| **다채널 입력** | ❌ (터미널만) | ✅ Telegram/Discord/Slack/Web | ✅ 동일 |
| **다 provider** | ❌ | ✅ | ❌ (자체 호스팅 1개) |
| **모델 가중치 baseline** | ❌ | ❌ | ✅ |
| **Logit-level forensic** | ❌ | ❌ | ✅ |
| **GPU / KV cache server metrics** | ❌ | ❌ | ✅ |
| **Aegis 활용도 점수 (1-5)** | 3.0 | 3.5 | **4.5** |
| **적합 사용자** | 솔로 개발자 | 다채널 운영, vendor lock-in 회피 | 정부/방위/금융/의료, privacy-first |
| **상세 매뉴얼** | [📘 CLAUDE_CODE.ko.md](CLAUDE_CODE.ko.md) | [📘 OPENCLAW_CLOUD.ko.md](OPENCLAW_CLOUD.ko.md) | [📘 OPENCLAW_LOCAL.ko.md](OPENCLAW_LOCAL.ko.md) |

## 공통 — 어느 트랙에서나 작동

세 트랙 모두 같은 코어를 공유하므로 다음은 모두 동일하게 사용 가능:

- **🏋️ ATV Coach** — burn-in 5-layer × 4-phase 학습 + case-memory RAG
- **📊 ATV Live** — `aegis report`, `aegis cost summary`, `aegis fleet-monitor`, ATMU
- **🔧 ATV Doctor** — `aegis forensic`, `aegis advise`, `aegis rollback`, `aegis health`
- **Audit chain** — SHA3 + opt-in Ed25519 (`aegis audit-key init`)
- **16-step firewall** — step305 → step340

자세한 사용법: [기능별 매뉴얼](../manuals/README.md)

## 트랙별 추가 능력

위 공통 기능 *위에*, 각 트랙이 추가로 하는 것:

### 🟢 Claude Code 트랙
- Claude Code 의 hooks 5종 (PreToolUse / PostToolUse / Stop / PreCompact / SessionStart / UserPromptSubmit)
- 5개 슬래시 커맨드 (`/aegis-report`, `/aegis-verify`, `/aegis-advise`, `/aegis-forensic`, `/aegis-help`)
- Homebrew / pip / source 3가지 distribution

### 🟡 OpenClaw + Cloud LLM 트랙 (Preview)
- 다채널 attribution (Telegram/Discord/Slack/Web)
- Param rewrite (자동 redaction, sanitize)
- Provider drift 감지 (Anthropic ↔ OpenAI ↔ Google)
- Skill manifest baseline + ClawHub typosquatting 감지
- `requireApproval` 자동 timeout 추천

### 🟡 OpenClaw + Local OSS LLM 트랙 (Preview)
- 위의 OpenClaw 트랙 모든 능력 +
- **모델 가중치 hash baseline** + 양자화 drift 감지
- **Logit-level forensic** (vLLM `--return-logits` 활성 시)
- **Server-side KV cache hit/miss** (vLLM `/metrics` scrape)
- **GPU / HBM / throughput** 메트릭
- **Air-gapped 운영 가능** — 외부 호출 0

## 트랙 선택 후 다음 단계

선택한 트랙의 매뉴얼로 이동:

| 선택 | 다음 |
|------|------|
| 🟢 Claude Code | [CLAUDE_CODE.ko.md](CLAUDE_CODE.ko.md) → 5분 설치 |
| 🟡 OpenClaw + Cloud | [OPENCLAW_CLOUD.ko.md](OPENCLAW_CLOUD.ko.md) → Preview 안내 |
| 🟡 OpenClaw + Local | [OPENCLAW_LOCAL.ko.md](OPENCLAW_LOCAL.ko.md) → Preview 안내 |

기능별 매뉴얼은 트랙 선택과 무관하게:
- [🏋️ COACH_MANUAL.ko.md](../manuals/COACH_MANUAL.ko.md)
- [📊 LIVE_MANUAL.ko.md](../manuals/LIVE_MANUAL.ko.md)
- [🔧 DOCTOR_MANUAL.ko.md](../manuals/DOCTOR_MANUAL.ko.md)

## Roadmap 요약

| 시기 | 마일스톤 |
|------|----------|
| 2026 Q2 (현재) | 🟢 Claude Code 트랙 GA · OpenClaw 트랙 stub + 매뉴얼 |
| 2026 H1 | 🟡 OpenClaw + Cloud LLM 트랙 GA (`@openclaw/plugin-aegis` 출시) |
| 2026 H2 | 🟡 OpenClaw + Local OSS LLM 트랙 GA (vLLM metrics 통합 + 모델 baseline) |

각 마일스톤은 GitHub Project board 에서 추적: [github.com/happyikas/Aegis-ATV/projects](https://github.com/happyikas/Aegis-ATV/projects)
