# Aegis ATV — 사용 설명서

> **이 문서가 누구를 위한 것인가요?** 코드를 잘 모르는 분도 5–10분 안에 "Aegis 가 무엇이고, 어떻게 쓰는지" 이해할 수 있게 작성한 통합 가이드입니다. 기능별 깊은 매뉴얼은 [`docs/manuals/`](manuals/README.md) 에 있습니다.

---

## 1. 한 페이지 요약

### 무엇을 하는 도구입니까?

**Aegis 는 AI 에이전트가 실수 (또는 공격) 로 시스템을 망가뜨리지 못하게 막고, 동시에 모든 행동을 위조 불가능한 기록으로 남기는 도구입니다.**

비유로 설명하면:
- 🚪 **자물쇠** — AI 에이전트가 위험한 명령을 실행하기 직전에 막습니다
- 📹 **CCTV** — AI 에이전트의 모든 행동을 시간 순서대로 기록합니다
- 🧾 **공증된 영수증** — 그 기록은 암호로 서명되어 있어 사후에 누구도 위조 못 합니다

### 어디에 끼어드나요?

```
  ┌──────────────┐
  │  AI 에이전트  │   ← Claude Code, OpenClaw, Codex, 자체 봇 …
  └──────┬───────┘
         │ "이 도구를 호출하겠다"
         ▼
  ┌──────────────┐
  │  ★ Aegis     │   ← 자물쇠 + CCTV + 영수증
  │              │     "이 행동 안전한가?" → 통과 / 승인 요청 / 차단
  └──────┬───────┘
         │ (안전한 경우만)
         ▼
  ┌──────────────┐
  │  실제 도구    │   ← 셸 명령, DB 조회, API 호출, 결제 …
  └──────────────┘
```

이 위치를 PitchDeck 에서는 **"below the model — between decision and execution"** 이라고 부릅니다. 모델의 안전 응답 필터 (예: Claude 의 답변 거부) 보다 **한 단계 더 아래** 에서 작동합니다.

### 한 줄 설치

```bash
uv run aegis install --mode local
```

→ 이 한 줄이면 Claude Code 가 만드는 모든 도구 호출이 Aegis 를 거칩니다. 무료, 외부 호출 0, 데이터는 노트북 밖으로 안 나갑니다.

---

## 2. 어떤 사람에게 필요한가요?

| 당신은 | Aegis 가 해주는 것 |
|---|---|
| 🧑‍💻 **Claude Code 일상 사용 개발자** | "삭제하지 말아야 할 파일을 AI 가 지우려 할 때" 자동 차단 + 모든 작업의 자동 기록 |
| 🏥 **병원 / 금융 / 정부 등 규제 산업** | EU AI Act / HIPAA / SOC 2 가 요구하는 **변조 불가능한 감사 로그** 제공 |
| 🤖 **AI 에이전트를 만드는 개발자** | OpenClaw / 자체 프레임워크에 보안 + 감사 layer 를 코드 한 줄로 추가 |
| 🛡️ **기업 보안 / 컴플라이언스 팀** | 여러 AI 도구 × 여러 LLM provider 의 행동을 **하나의 대시보드** 에서 비교 |
| 🚀 **multi-LLM 환경 운영자** | OpenRouter / 멀티 provider 환경에서 *어느 provider 가 더 위험한지* 정량 측정 |

---

## 3. 한 시나리오로 보는 동작 — "삭제 사고 방지"

**상황**: 당신이 Claude Code 에게 "tmp 폴더 좀 정리해줘" 라고 부탁. AI 가 잘못 해석해서 시스템 폴더를 통째로 삭제하려고 함.

**Aegis 없이**:
```
사용자: "tmp 정리해줘"
Claude:  → 위험한 재귀 삭제 명령 실행 (시스템 폴더 대상)
         → 실행됨, 시스템 망가짐
```

**Aegis 와 함께**:
```
사용자: "tmp 정리해줘"
Claude:  → 같은 위험 명령 시도 → Aegis 가 가로챔
Aegis:   ⛔ BLOCK  trace=abc123  (45ms)
         reason: dangerous pattern: 시스템 경로 대상 재귀 삭제
         advise: [HIGH] security-reviewer — Block until reviewer ACKs
사용자:  (안 망가짐. audit log 에 시도 기록됨.)
```

이 한 동작에서:
- ⚡ **45ms 안에 차단** — 사용자는 "AI 가 느려진" 느낌 없음
- 📝 **audit log 에 영구 기록** — 사후 분석 가능
- 🤖 **8 명의 가상 advisor** 가 자동으로 권고 — `security-reviewer`, `cost-optimizer`, `loop-breaker` 등 위험 영역별 전문 조언

> 💡 *이 문서를 작성하는 동안 실제로 Aegis 의 firewall 이 작동했습니다. 이 섹션의 처음 예시는 destructive pattern 정규식에 걸려 문서 저장이 차단되었습니다 — Aegis 가 자기 자신을 안전하게 보호하는 증거.*

---

## 4. 5분 설치 — 3가지 옵션 중 하나

### 옵션 A — Solo 개발자 (가장 간단)

```bash
# 1) 저장소 클론 + 의존성
git clone https://github.com/happyikas/Aegis-ATV.git
cd Aegis-ATV
uv sync

# 2) Claude Code 에 Aegis 후크 설치
uv run aegis install --mode local

# 3) Claude Code 재시작 — 끝
```

이게 PitchDeck 의 **"Solo Free"** 입니다. 비용 0원, 외부 호출 0, 데이터는 본인 머신 안에서만.

### 옵션 B — 한 줄 설치 (저장소 클론 없음)

```bash
curl -LsSf https://raw.githubusercontent.com/happyikas/Aegis-ATV/main/scripts/install.sh | bash
```

### 옵션 C — Mac 사용자 (Homebrew)

```bash
brew tap happyikas/aegis https://github.com/happyikas/Aegis-ATV.git
brew install happyikas/aegis/aegis
aegis install --mode local
```

### 옵션 D — 기업 / 멀티 사용자 (Sidecar 모드)

여러 사람이 같은 Aegis 인스턴스를 사용해야 하는 환경:

```bash
docker compose up -d
# → localhost:8000 에서 FastAPI 서비스 가동
# → 모든 클라이언트가 이 service 를 통해 검증
```

---

## 5. 처음 5가지 명령어

설치 후 가장 먼저 익혀야 할 다섯 가지:

| 명령 | 설명 | 언제 쓰나 |
|---|---|---|
| `aegis status` | 현재 설치 상태 + 운영 통계 | 매일 첫 명령 |
| `aegis report` | 최근 24시간의 5줄 위험 요약 | 매일 / 매주 점검 |
| `aegis verify-audit` | 감사 체인이 위조되지 않았는지 검증 (1초) | 사건 의심 시 / 매주 |
| `aegis forensic last` | 가장 최근 BLOCK / REQUIRE_APPROVAL 케이스 자세히 보기 | "왜 차단됐지?" |
| `aegis advise` | AI 가 권고한 행동 (cost / security / performance) 종합 | 운영 개선 |

### 예시 — `aegis report` 출력

```
$ aegis report

🛡️  Aegis Risk Report  (last 24h)

  Calls: 1,243   ALLOW 1,198 / APPROVAL 38 / BLOCK 7
  Top risks:
    • 4× destructive-bash (production folder)
    • 2× credential-leak attempts
    • 1× cost spike (Claude → GPT-4 retry loop)

  Cost: $4.18 (Claude $3.92 + GPT-4 $0.26)
  Audit chain: ✓ intact, 12 sessions

  next: `aegis forensic last` to inspect the latest BLOCK
```

5 줄로 "어제 무슨 일이 있었나" 파악 — PitchDeck 에서 강조한 *Observability* 부분의 일상 사용 화면.

---

## 6. 3가지 핵심 기능 — Coach · Live · Doctor

Aegis 는 PitchDeck 의 5 가지 기반 기술 (ATV / ATMU / sLLM / Crypto-Sign / Burn-in) 을 **3 개 사용자 기능** 으로 묶어 제공합니다. 각각 한 문단 + 깊은 매뉴얼 link.

### 🏋️ ATV Coach — "내 환경의 정상 / 이상 학습"

당신 환경의 **정상적인 도구 사용 패턴**을 5단계 × 4 phase 로 자동 학습합니다. 처음에는 *Observation* 단계 (관찰만), 충분히 모이면 *Shadow → Assisted → Production* 으로 점진 승격. 학습된 baseline 이 sLLM judge 에 주입되어 "이 도구 호출이 평소와 다른지" 빠르게 판단.

```bash
aegis burnin status         # 학습 진행도
aegis burnin train-m13      # baseline 재학습
aegis baseline reattest     # baseline 갱신 적용
```

→ [`docs/manuals/COACH_MANUAL.ko.md`](manuals/COACH_MANUAL.ko.md)

### 📊 ATV Live — "지금 무슨 일이 벌어지나"

agent 의 **Cost / Performance / Security** 를 실시간으로 추적합니다. "이번 주 비용 분석", "어떤 agent 가 가장 위험한가", "어떤 LLM provider 가 BLOCK rate 가 높은가" 같은 질문에 답합니다.

```bash
aegis report                                # 5 줄 요약
aegis report --by-aid                       # agent 별 분리
aegis report --by-provider                  # LLM provider 별 (Claude vs GPT)
aegis report --by-aid-and-provider          # 교차 분석
aegis cost summary --since 7d               # 주간 비용
aegis fleet-monitor start                   # 알림 daemon (Slack/ntfy)
```

→ [`docs/manuals/LIVE_MANUAL.ko.md`](manuals/LIVE_MANUAL.ko.md)

### 🔧 ATV Doctor — "사건이 일어났을 때 진단 + 치료"

뭔가 잘못됐을 때 **무엇이 일어났는지 정확히 재현** + **다음에 어떻게 막을지 권고** + **시간 되돌리기**.

```bash
aegis forensic last                # 최근 BLOCK 분석
aegis advise <trace_id>            # 그 케이스의 advisor 권고
aegis rollback <trace_id>          # 그 시점으로 시스템 상태 되돌리기
aegis verify-audit                 # 감사 체인 무결성 검증
```

→ [`docs/manuals/DOCTOR_MANUAL.ko.md`](manuals/DOCTOR_MANUAL.ko.md)

---

## 7. PitchDeck 의 5 기술 — 코드 위치 매핑

| PitchDeck 명칭 | 무엇인가 (한 줄) | 코드 위치 |
|---|---|---|
| **ATV** (Agent Telemetry Vector) | 모든 agent 행동을 2,048-D 벡터로 인코딩 — 30 개 명시 subfield + 학습된 임베딩 | `src/aegis/schema.py` |
| **ATMU** (Action Trust Management Unit) | 도구 실행 전에 정책을 적용하는 게이트 — 16 단계 firewall | `src/aegis/firewall/step3*.py` |
| **sLLM Judgment Engine** | 양자화된 3B 로컬 모델이 ambiguous case 에 대한 second opinion 제공 — 0 cloud egress | `src/aegis/judge/`, `src/aegis/firewall/step340.py` |
| **Crypto-Sign** | SHA3 hash chain + Ed25519 서명 — 1 명령으로 외부 감사 가능 | `src/aegis/audit/`, `aegis verify-audit` |
| **Burn-in** | 매 릴리스 1k+ 적대적 시나리오 재현 — 서명된 scorecard 없이 빌드 안 나감 | `src/aegis/burnin/` |

비전문가용 한 문장으로 통합:
> **5 가지 기술이 합쳐서**: agent 의 모든 행동을 **표준 벡터** 로 인코딩하고 (ATV), 그 벡터에 **정책을 적용** 하고 (ATMU), 애매한 케이스는 **로컬 AI 가 검토** 하고 (sLLM), 모든 결정을 **암호로 서명된 체인** 에 기록 (Crypto-Sign), 매 릴리스마다 **공격 시나리오로 재검증** (Burn-in).

---

## 8. 요금제 — Solo Free vs Pro vs Team vs Enterprise

PitchDeck 의 commercial offering boundary 와 매칭:

| | **Solo Free** | **Pro** | **Team** | **Enterprise** |
|---|---|---|---|---|
| 가격 | **무료 (영구)** | $19/월 | $39/seat/월 | 별도 |
| 라이선스 | Apache-2.0 | 상용 | 상용 | 상용 |
| 16-step Firewall | ✅ | ✅ | ✅ | ✅ |
| 감사 체인 | ✅ | ✅ | ✅ | ✅ |
| 8-advisor pipeline (Coach + Live + Doctor) | ❌ (advisor OFF) | ✅ | ✅ | ✅ |
| sLLM judge (로컬 Phi-3) | ❌ (dummy 룰만) | ✅ | ✅ | ✅ |
| Haiku judge (cloud) | ❌ | ✅ | ✅ | ✅ |
| Sidecar (멀티 사용자) | ❌ | ❌ | ✅ | ✅ |
| 우선 지원 + SLA | ❌ | ❌ | ❌ | ✅ |

핵심: **무료 tier 도 단독으로 의미 있게 작동** 합니다. 본인 노트북, 본인 데이터, 외부 호출 0. PitchDeck 의 "Solo Free unconditionally free forever (Apache-2.0)" 약속.

```bash
# 라이선스 활성화 (Pro 이상)
aegis license activate ~/Downloads/my-key.jwt

# 현재 상태 확인
aegis license status
```

→ 자세히: [`PRICING.md`](../PRICING.md) + [`docs/LICENSE_KEY.md`](LICENSE_KEY.md)

---

## 9. 통합 시나리오 — "어떤 도구 / 환경에서 쓰나"

### Claude Code 사용자 (가장 흔함)

```bash
uv run aegis install --mode local --profile pro
# → ~/.claude/settings.json 자동 패치
```

이후 Claude Code 가 만드는 모든 tool call (Bash / Edit / Read / MCP / …) 이 Aegis 를 거칩니다. 사용자 추가 작업 없음.

### OpenClaw 사용자 (멀티 채널 agent — Telegram/Discord/Slack/CLI)

OpenClaw 프로젝트에서:

```bash
npm install @happyikas/openclaw-plugin-aegis
```

```typescript
// plugins/aegis/index.ts
import { activate } from "@happyikas/openclaw-plugin-aegis";
export default activate;
```

Aegis sidecar 가 띄워져 있으면 모든 OpenClaw tool call 이 자동 검증.

→ 자세히: [`docs/integrations/openclaw.md`](integrations/openclaw.md)

### OpenRouter 사용자 (300+ models, 60+ providers)

```python
from openai import OpenAI
from aegis.integrations.openrouter import canonical_provider

client = OpenAI(base_url="https://openrouter.ai/api/v1", ...)
resp = client.chat.completions.create(model="anthropic/claude-sonnet-4", ...)

# Aegis 에 OpenRouter 의 실제 provider 전달
provider_str = canonical_provider(resp.model_dump())
# → "openrouter:anthropic-claude-sonnet-4"
```

이렇게 하면 `aegis report --by-provider` 가 OpenRouter route 별로 분리됩니다 — 같은 prompt 가 OpenAI 로 갔을 때 BLOCK rate 가 Claude 와 다르면 자동 경고.

→ 자세히: [`docs/integrations/openrouter.md`](integrations/openrouter.md)

### Hermes 사용자 (self-improving agent)

Hermes 처럼 스스로 학습 / 개선하는 agent 환경에서 Aegis 는 **외부 감사 layer** 역할:

> *"agent 가 자기 코드/skill 을 갱신할 때, 그 갱신이 안전한 방향인지 누가 검증합니까?"* — Aegis 가 그 외부 검증입니다.

→ 자세히: [`docs/integrations/hermes.md`](integrations/hermes.md)

---

## 10. 자주 묻는 질문

### Q1. 내 데이터가 외부로 나가나요?

**Solo Free / Pro / Team 모두**: `--mode local` 사용 시 **0 byte 가 외부로 안 나갑니다**. Aegis 사이드카는 localhost 에서만 작동, 감사 로그도 본인 머신의 `~/.aegis/audit.jsonl` 에만 저장.

`--profile pro / cloud` 의 경우 *애매한 케이스만* Haiku judge 에 보낼 수 있는데, 이건 OPT-IN 이고 `AEGIS_JUDGE_PROVIDER=local-phi` 로 끌 수 있습니다.

규제 산업: `--mode local --profile pro` + `AEGIS_JUDGE_PROVIDER=local-phi` = **외부 호출 영구 0**.

### Q2. 너무 느리지 않을까요?

Solo Free: 평균 5ms (dummy 룰), 95% latency < 10ms.
Pro / Team: 평균 50ms (sLLM judge 포함, p95).
PitchDeck 의 *"< 50ms p95 — agent never feels it"* 와 매칭.

### Q3. Claude Code 가 이미 안전 응답 (refuse) 을 하는데 또 필요한가요?

Claude 의 refuse 는 **모델 출력 수준** 의 가드. Aegis 는 **모델 출력 이후, 실제 tool 실행 직전** 에 또 한 번 검증합니다. PitchDeck 의 *"below the model"* 포지셔닝 — 모델이 의도하지 않은 위험 실행을 잡습니다 (예: 정상 prompt 이지만 컨텍스트 손상으로 destructive args 생성).

### Q4. 라이선스가 만료되면?

Pro+ 기능 (advisor, sLLM judge) 만 비활성되고 **Solo Free 동작은 그대로 유지**. 기존 audit log 도 영원히 본인 소유 — 외부에 송신 안 됨.

### Q5. 기존 OpenAI / Anthropic / vLLM 환경과 충돌하나요?

없음. Aegis 는 tool call layer 에만 끼어들고, LLM 호출 자체는 안 건드립니다. **OpenRouter, Claude, GPT, Gemini, vLLM, Ollama 모두 동시 사용 가능** — 오히려 `--by-provider` cross-grouping 으로 *어느 LLM 이 더 위험한지* 정량 비교 가능.

### Q6. 감사 로그가 진짜 위조 불가능한가요?

네. PitchDeck 의 Crypto-Sign 부분:

1. 매 레코드가 SHA3 hash 로 이전 레코드에 연결 (chain-of-custody)
2. 매 레코드가 Ed25519 로 서명 — Aegis 의 비밀키 없이는 위조 불가
3. `aegis verify-audit` 한 명령으로 외부 검증 가능 — 1 초 이내

수정 / 삭제 시도 시 `verify-audit` 가 즉시 실패. SOC 2 / HIPAA / EU AI Act 가 요구하는 *tamper-evident* 요건 충족.

### Q7. Open Source 인가요?

**Solo Free** = Apache-2.0, 영구 무료. 코드 전체 GitHub 공개.
**Pro / Team / Enterprise** = 같은 코드, 라이선스 키로만 추가 기능 (advisor, sLLM, sidecar) 활성. PitchDeck 의 "no rugpull-bait" 약속.

---

## 11. 자주 발생하는 문제 + 해결

| 증상 | 원인 / 해결 |
|---|---|
| `aegis install` 후 Claude Code 가 후크 무시 | Claude Code **재시작** 필수. 그래도 안 되면 `aegis status` 로 settings.json 패치 상태 확인 |
| `aegis verify-audit` 실패 | 누군가 ~/.aegis/audit.jsonl 을 수정했을 가능성. `aegis forensic last` 로 마지막 정상 레코드 확인 → 해당 시점 이후 backup 으로 복원 |
| `BLOCK` 메시지가 너무 많이 뜸 | Coach baseline 학습이 부족. 1주일 정도 `AEGIS_BURNIN_SHADOW=1` 운영 후 `aegis burnin train-m13` |
| Pro 라이선스 활성화 후에도 dummy judge 사용 | 환경 변수 확인: `echo $AEGIS_JUDGE_PROVIDER`. `unset` 또는 `local-phi` / `haiku` 로 설정 |
| OpenRouter route 가 `(no-provider)` 로 잡힘 | `aegis.integrations.openrouter.canonical_provider()` 헬퍼로 provider 문자열 생성하여 ATV header 에 stamp 필요 |
| 한국어 메시지 깨짐 (Windows) | 터미널 UTF-8 설정: `chcp 65001` |

깊은 진단은: [`docs/manuals/DOCTOR_MANUAL.ko.md`](manuals/DOCTOR_MANUAL.ko.md) §5 "trouble shooting"

---

## 12. 다음 단계

### 사용자 수준별 다음 행동

| 당신의 상태 | 다음 한 단계 |
|---|---|
| 막 설치한 Solo Free 사용자 | 일주일 사용 후 `aegis report --since 7d` — 본인 사용 패턴 살펴보기 |
| Solo Free 인데 advisor 기능 궁금 | Pro 시범 — [`PRICING.md`](../PRICING.md) 의 design partner 안내 |
| 기업 / 멀티 사용자 도입 검토 | [`docs/DESIGN_PARTNER_PROGRAM.md`](DESIGN_PARTNER_PROGRAM.md) — 30일 무료 pilot |
| 다른 agent 프레임워크 통합 | [`docs/integrations/`](integrations/) — OpenClaw / OpenRouter / Hermes / Paperclip 각각 |
| 깊은 기술 문서 | [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) — STRIDE walk + auditor checklist |

### 영상 / 데모 자료

- 빠른 시연 GIF: [`demo/recording/quickstart.gif`](../demo/recording/quickstart.gif)
- 1 페이지 데모 시나리오: [`demo/`](../demo/)
- NVIDIA Inception PitchDeck: 본 가이드 작성 시 참조한 자료 (외부 자산)

### 커뮤니티

- GitHub: https://github.com/happyikas/Aegis-ATV
- 사이트: https://aegisdata.ai
- 라이선스 / 영업 문의: `datamonster@aegisdata.ai`

---

## 13. 한 문장 요약 — 다시

> **Aegis 는 당신의 AI 에이전트가 만드는 모든 행동을, 실행 직전에 검증하고, 영원히 위조 불가능한 기록으로 남기는 도구입니다.** 무료로 시작하고, 노트북 밖으로 데이터가 나가지 않으며, 한 명령으로 감사 가능합니다.

설치는 한 줄:

```bash
uv run aegis install --mode local
```

추가 질문이 있으면 위 §10 FAQ 또는 [`docs/manuals/`](manuals/) 의 기능별 매뉴얼을 참조하세요.
