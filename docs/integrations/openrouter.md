# Aegis × OpenRouter — 통합 가이드

**작성**: 2026-05-14
**상태**: GA — Python 헬퍼 (`aegis.integrations.openrouter`) + docs
**대상**: [OpenRouter](https://openrouter.ai) — 300+ 모델 × 60+ provider 를 단일 OpenAI-호환 API 로 라우팅하는 LLM gateway

---

## 1. 한 줄 요약

> **OpenRouter 는 LLM call 의 routing/billing layer. Aegis 는 tool call 의 firewall/audit layer.** 둘은 다른 레이어에 있어 **경쟁 없이 보완**합니다. 같이 쓰면: OpenRouter 가 "어떤 model 이 이 prompt 를 처리?" 를, Aegis 가 "그 결과 tool call 이 안전한가?" 를 답합니다.

---

## 2. 왜 둘을 같이 쓰나

| 사용자가 원하는 것 | OpenRouter 단독 | Aegis 단독 | 결합 |
|---|---|---|---|
| 300+ 모델 사이 동적 라우팅 | ✅ | ❌ | ✅ |
| 지출 한도 / 모델 allowlist | ✅ (account level) | 🟡 (per-call cost ledger) | ✅ |
| Tool call 위험 평가 (destructive command, credential leak, …) | ❌ | ✅ (16-step firewall) | ✅ |
| Cryptographic audit chain (SHA3 + Ed25519) | ❌ | ✅ | ✅ |
| Provider-divergence 감지 ("Claude ALLOW, GPT BLOCK") | ❌ | ✅ (provider-drift advisor) | ✅ **둘이 있을 때만 의미 있음** |
| Air-gapped audit (외부 0 byte) | ❌ (cloud gateway) | ✅ (local mode) | OpenRouter `zdr: true` + Aegis local-mode 콤보 |

핵심: provider-divergence advisor 는 **여러 provider 가 같은 prompt 를 처리하는 환경**에서만 의미가 있는데, OpenRouter 가 정확히 그 환경을 만듭니다.

---

## 3. 3-Layer Stack 도해

```
┌─────────────────────────────────────────────────────────┐
│  User / channel  (Telegram, Discord, Slack, CLI, web)   │
└────────────────────┬────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────┐
│  Agent runtime   (OpenClaw / Claude Code / custom)      │
│                  ↳ before_tool_call → Aegis hook        │
└────────────────────┬────────────────────────────────────┘
                     ▼  LLM call
┌─────────────────────────────────────────────────────────┐
│  ★ OpenRouter        300+ models, 60+ providers         │
│                      provider routing, BYOK, ZDR        │
│                      response.provider_responses[].name │
│                          ← 어느 provider 가 실제 처리?   │
└────────────────────┬────────────────────────────────────┘
                     ▼  model response (tool calls)
┌─────────────────────────────────────────────────────────┐
│  ★ Aegis ATV         16-step firewall                   │
│                      SHA3+Ed25519 audit chain           │
│                      8-advisor pipeline                 │
│                      provider="openrouter:<vendor>-<m>" │
└────────────────────┬────────────────────────────────────┘
                     ▼  approved / rewritten / blocked
                  tool execution (Bash, Edit, …)
```

---

## 4. 코드 — Python 헬퍼

설치 후 (`uv pip install aegis-mvp>=0.3.1`) 바로 사용 가능:

```python
import os
import json
import uuid

import httpx
from openai import OpenAI

from aegis.integrations.openrouter import canonical_provider

# 1) OpenAI SDK 를 OpenRouter base_url 로 사용 (일반 패턴)
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)
response = client.chat.completions.create(
    model="anthropic/claude-sonnet-4",
    messages=[...],
    extra_body={"provider": {"data_collection": "deny"}},
)

# 2) Aegis 용 canonical provider 문자열
provider_str = canonical_provider(response.model_dump())
# → "openrouter:anthropic-claude-sonnet-4"

# 3) Aegis evaluate 호출 시 header.provider 에 넣기
aegis_resp = httpx.post(
    "http://localhost:8000/evaluate/openclaw",
    json={
        "tool_name": "Bash",
        "tool_args_json": json.dumps({"command": "git push origin main"}),
        "header": {
            "aid": "agent-x",
            "trace_id": str(uuid.uuid4()),
            "channel": "telegram",
            "provider": provider_str,   # ← 여기
        },
    },
)
```

### 4.1 OpenRouter fallback chain 추적

라우팅 실패가 있으면 (예: AnthropicVertex 503 → Anthropic 200) 그 정보가 운영에 유용합니다:

```python
from aegis.integrations.openrouter import parse_response, provider_chain

call = parse_response(response.model_dump())
print(call.provider_string)          # "openrouter:anthropic-claude-sonnet-4"
print(call.is_fallback)              # True
print(provider_chain(call))          # "AnthropicVertex(503) → Anthropic(200)"
print(call.attempts[0].is_success)   # False
print(call.attempts[-1].is_success)  # True
```

운영 권장: `is_fallback=True` 일 때 운영 로그에 chain 한 줄 같이 남기기. Aegis 의 reliability advisor 가 이 신호를 활용할 수 있는 surface 는 다음 PR 후보.

### 4.2 헬퍼 API 표

| 함수 / 클래스 | 목적 | 입력 | 출력 |
|---|---|---|---|
| `canonical_provider(response, headers=None)` | 가장 흔한 호출 — 한 줄 사용 | dict / SDK object | `"openrouter:<vendor>-<model>"` |
| `parse_response(response, headers=None)` | 전체 구조 필요 시 | 동상 | `OpenRouterCall` |
| `OpenRouterCall.provider_string` | canonical 문자열 | — | str |
| `OpenRouterCall.is_fallback` | 2+ 시도 있었는지 | — | bool |
| `OpenRouterCall.model_slug` | "/" 뒤 모델명 | — | str |
| `OpenRouterCall.attempts` | 시도 chain | — | `tuple[ProviderAttempt, ...]` |
| `provider_chain(call)` | 사람-읽기 chain 문자열 | `OpenRouterCall` | str |
| `ProviderAttempt.is_success` | HTTP 2xx 인가 | — | bool |

### 4.3 canonical 문자열 포맷 규칙

```
openrouter:<vendor>-<model>
```

- `vendor` = `provider_responses[]` 의 마지막 성공한 entry 의 `name` 을 lowercase + hyphen-form 으로 정규화
- `model` = 요청 model slug 의 `/` 뒤 부분 (소문자)
- 한 단어 acronym 은 분리 안 함 (예: `OpenAI` → `openai`, `xAI` → `xai`)
- 복합 단어는 분리 (예: `AnthropicVertex` → `anthropic-vertex`, `DeepInfra` → `deep-infra`)
- `provider_responses` 없으면 model slug 의 vendor prefix 로 fallback
- 어떤 입력에도 raise 하지 않음 — 최악의 경우 `"openrouter:unknown"` return

---

## 5. `aegis report --by-provider` 결과

Aegis 의 audit 가 `provider="openrouter:..."` 를 캡처하면 cross-grouping 이 자동으로 OpenRouter route 별로 분리됩니다:

```
$ aegis report --by-provider --since 7d

AegisData Agent Risk Report — by provider
=========================================
  window:    last 10080 min
  audit log: ~/.aegis/audit.jsonl  (3421 entries, 4 providers)

  openrouter:anthropic-claude-sonnet-4     1842 total  ALLOW 1601 / APPROVAL 198 / BLOCK 43
  openrouter:openai-gpt-4o                  834 total  ALLOW  701 / APPROVAL 117 / BLOCK 16
  openrouter:deepinfra-llama-3.3-70b        612 total  ALLOW  502 / APPROVAL  98 / BLOCK 12
  openrouter:google-gemini-1.5-pro          133 total  ALLOW  118 / APPROVAL  12 / BLOCK  3

  ⚠ provider-drift advisor:
      openrouter:openai-gpt-4o BLOCK rate (1.92%) is 3.1× the cross-provider
      median (0.62%). Investigate whether OpenAI's safety responses are
      over-blocking valid calls, or whether this provider is being asked
      to handle different risk profiles.
```

이 결과가 OpenRouter user 에게 즉시 의미 있는 이유:
- 같은 prompt 가 cost-optimization 으로 OpenAI 로 라우팅됐을 때 BLOCK rate 가 다르면 **safety drift**
- vendor lock-in 없이 multi-LLM 도입한 환경의 진짜 risk surface

---

## 6. Setup — 처음부터 끝까지

### 6.1 OpenRouter 측 권장 설정

```python
extra_body = {
    "provider": {
        "data_collection": "deny",     # 학습 데이터 수집 거부 provider 만
        "zdr": True,                    # Zero Data Retention 강제
        "sort": "throughput",           # 또는 "price" / "latency"
    },
}
```

Regulated 산업 (fintech / health / defense) 이면 위 3 옵션 다 켜기.

### 6.2 Aegis 측 권장 설정

```bash
# Solo Free / Pro — local mode (외부 호출 0)
uv run aegis install --mode local --profile pro
# 또는 sidecar (멀티 테넌트)
uv run aegis install --mode sidecar
docker compose up -d
```

OpenRouter `zdr: true` + Aegis `--mode local` 콤보 = **외부 0 byte + cryptographic audit chain**. 규제 산업 셋업의 정확한 spec.

### 6.3 Burn-in observation 단계로 시작

처음 도입 시 OpenRouter route 마다 baseline 이 다를 수 있으므로:

```bash
export AEGIS_BURNIN_SHADOW=1
# 1주일 운영 → shadow.jsonl 누적
aegis burnin status
aegis burnin train-m13     # 사람 라벨 추가 후 retrain (PR #169 의 aegis label CLI)
aegis baseline reattest
```

provider-drift advisor 는 (aid, provider) tuple baseline 이 충분히 모인 후 작동합니다.

---

## 7. 정직한 scope — 미구현 / 한계

| 항목 | 현재 | 다음 |
|---|---|---|
| `provider_responses` body 파싱 | ✅ (`parse_response` 헬퍼) | — |
| `x-openrouter-provider` 헤더 fallback | ✅ | — |
| OpenRouter fallback chain → Aegis advisor signal | ❌ — `is_fallback` 노출만 됨 | reliability advisor v2 |
| Cost ledger 가 OpenRouter `/api/v1/generation` 메타데이터 cross-verify | ❌ | `aegis cost openrouter-verify` 후보 |
| OpenClaw plugin TypeScript 측 자동 stamp | ❌ — Python 헬퍼만 | `@happyikas/openclaw-plugin-aegis@0.4.0` 후보 |
| `aegis report --by-openrouter-route` 전용 view | ❌ | `--by-provider` 가 이미 prefix 그룹핑 가능 |
| Activity export CSV → Coach burn-in 부트스트랩 | ❌ | post-MVP |

---

## 8. 다른 통합 문서

- [`hermes.md`](hermes.md) — self-improving agent 환경에서 Aegis 가 외부 감사 layer
- [`openclaw.md`](openclaw.md) — OpenClaw 환경 분석. OpenClaw 도 OpenRouter 와 공식 통합되어 있어 (OpenClaw → OpenRouter → Aegis) 3-layer 가 자연
- [`paperclip.md`](paperclip.md) — 다른 agent runtime 의 통합 분석

---

## 9. Show HN / 영업용 한 줄

> "OpenRouter 가 LLM 선택을 추상화하면, 같은 prompt 가 여러 provider 로 라우팅됩니다. 그러면 *어느 provider 가 더 안전한가* 라는 질문이 처음으로 측정 가능해집니다. Aegis 의 provider-drift advisor 가 OpenRouter 환경에서만 발휘되는 unique value 입니다."

이 narrative 가 Show HN 본문 + design partner outreach 에 그대로 쓰일 수 있습니다.
