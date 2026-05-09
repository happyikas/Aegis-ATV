# Aegis ATV — OpenClaw + Local OSS LLM 트랙 (한국어 정본)

> **상태**: 🟡 **Preview** (analysis 완료, 플러그인 구현 진행 중)
> **타겟**: 데이터 residency 가 필수인 사용자 / privacy 최우선 / power user
> **설치**: `aegis install --target openclaw-local` (현재 stub — Roadmap 참조)

---

## 1. 이 트랙이 누구를 위한 것인가

세 가지 조건 중 **하나라도** 해당되면 이 트랙이 맞습니다:

1. **데이터를 외부 cloud LLM 에 보낼 수 없다** — 회사 정책, 규제, 또는 개인 신념
2. **모델 자체를 자기 자산으로 다루고 싶다** — 가중치 파일 baseline, 양자화 drift 추적, 모델 supply chain 감사
3. **Inference 서버 측 metrics 를 직접 보고 싶다** — KV cache 진짜 hit/miss, GPU/HBM 사용률, throughput

이 트랙은 OpenClaw (오픈소스 agent 프레임워크) + 로컬 LLM 서버 (vLLM / Ollama / TGI) + Aegis 가 **한 머신 안에서** 다 도는 구조입니다. 외부 호출 0.

---

## 2. 왜 Aegis 가 이 트랙에서 가장 강한가

[OpenClaw 환경 비교 분석](../../docs/integrations/openclaw.md) 과 본 PR 의 사전 토론에서 도출된 결론 —
**Aegis 의 활용도가 Cloud LLM 트랙 대비 약 1.3 배 높음**. 이유:

| 영역 | Local OSS 에서 추가로 가능 |
|------|---------------------------|
| 🏋️ Coach | 모델 가중치 hash baseline, 양자화 drift 감지, jailbreak 학습 |
| 📊 Live | per-token latency 분해, GPU/CPU/HBM 사용률, speculative decoding hit rate |
| 🔧 Doctor | logit-level forensic, 모델 버전 롤백, hallucination 토큰 추적 |
| 🛒 Skill 보안 | ClawHub skill manifest baseline, supply chain 감지 (NPM/PyPI 류) |
| 🌐 다채널 | Telegram / Discord / Slack 채널 attribution, cross-channel injection 감지 |
| 🔒 Air-gapped | 외부 호출 0 — 정부 / 방위 / 금융 / 헬스케어 deployment 가능 |

이건 **시장에서 거의 유일한 air-gapped agentic AI 의 cryptographic 감사** 셋업입니다.

---

## 3. 권장 환경

### 3-1. 하드웨어

| 모델 크기 | 최소 GPU | 권장 GPU | 노트 |
|-----------|----------|----------|------|
| 1–3 B (Phi-3.5, Llama 3.2 1B) | M1/M2 16 GB 통합 | M-series 32 GB | 노트북 OK |
| 7–8 B (Llama 3.1 8B, Qwen 2.5 7B) | RTX 4060 8 GB | RTX 4080 16 GB | desktop |
| 14–34 B (Mistral, Qwen 32B, Llama 70B Q4) | RTX 4090 24 GB | A100 40 GB | workstation |
| 70 B+ (Llama 3.3 70B FP16) | A100 80 GB ×2 | H100 ×2+ | 서버 |

### 3-2. 소프트웨어 스택

```
┌─────────────────────────────────────────────────┐
│  Telegram / Discord / Slack / CLI 사용자        │
└──────────────────┬──────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────┐
│  OpenClaw agent (Node.js / TypeScript)          │
│  + @happyikas/openclaw-plugin-aegis  ← Aegis   │
└──────────────────┬──────────────────────────────┘
                   ▼ before_tool_call
┌─────────────────────────────────────────────────┐
│  Aegis sidecar (FastAPI, localhost:8000)        │
│  - 16-step firewall                             │
│  - SHA3 + Ed25519 audit chain                   │
│  - inference metrics scraper (vLLM /metrics)    │
└──────────────────┬──────────────────────────────┘
                   ▼ inference 호출
┌─────────────────────────────────────────────────┐
│  Local LLM 서버 (vLLM / Ollama / TGI)           │
│  - Llama / Qwen / Mistral / Phi GGUF 또는 SGL   │
└─────────────────────────────────────────────────┘
```

---

## 4. 무엇이 작동할 예정인가

### 4-1. 즉시 (Coach / Live / Doctor 의 base 기능)

Claude Code 트랙과 동일한 16-step firewall + audit chain + 8-advisor pipeline.

### 4-2. 이 트랙 *고유* 기능 (구현 예정)

| 기능 | 어디서 옴 |
|------|----------|
| 모델 가중치 hash baseline | step309 의 OpenClaw 버전 (가중치 파일 SHA3 → drift) |
| 양자화 drift 감지 | FP16 ↔ Q4 동일 prompt 응답 distribution 비교 |
| Logit-level forensic | vLLM `--return-logits` 활성 시 forensic 출력에 포함 |
| KV cache 진짜 hit/miss | vLLM metrics endpoint scrape → ATV `cache_hit_rate` 필드 |
| Speculative decoding stats | vLLM `--speculative-model` 모드의 acceptance rate |
| GPU 메트릭 | nvidia-smi / DCGM exporter 통합 |
| Jailbreak 출력 분포 학습 | Coach 의 새 layer L6 (출력 분포 anomaly) |
| ClawHub skill manifest baseline | step309 가 manifest hash 추적 |
| 채널-conditioned policy | per-channel baseline 분기 |

---

## 5. Roadmap (어디까지 됐고 무엇이 남았나)

| 단계 | 상태 | 설명 |
|------|------|------|
| 1. OpenClaw 분석 + 통합 가능성 검증 | ✅ 완료 | [PR #118](https://github.com/happyikas/Aegis-ATV/pull/118) |
| 2. `aegis install --target openclaw-local` stub | ✅ 완료 | [PR #127](https://github.com/happyikas/Aegis-ATV/pull/127) |
| 3. `@openclaw/plugin-aegis` TypeScript 스켈레톤 | ✅ 완료 | `openclaw-plugin/` — handler + HTTP client + 19 tests |
| 4. End-to-end OpenClaw runtime 통합 테스트 | 🟡 진행 중 | sidecar `/evaluate` ↔ 플러그인 |
| 5. vLLM metrics scraper | 🟡 진행 중 | `src/aegis/inference/vllm_metrics.py` |
| 6. 모델 가중치 baseline 통합 | 🔴 예정 | step309 의 OpenClaw 변형 |
| 7. Logit-level forensic | 🔴 예정 | vLLM logits 활성 시만 |
| 8. npm publish + ClawHub 마켓 등록 | 🔴 예정 | distribution channel |

→ 본 PR 머지 후 단계 3–7 은 **별도 PR 시리즈**로 진행. 사용자가 progress 를 추적할 수 있게 GitHub Project board 에 트래킹.

---

## 6. 지금 할 수 있는 것 (Preview 사용자)

이 트랙이 GA 가 될 때까지 **Claude Code 트랙으로 시작** 하는 것을 권장합니다 — 같은 16-step firewall + audit chain 을 즉시 사용 가능, 향후 OpenClaw 트랙 출시 시 audit log 그대로 import 가능 (호환 보장).

```bash
# 일단 Claude Code 트랙으로 익숙해지기
aegis install --target claude-code --mode local

# OpenClaw + Local OSS 가 GA 되면 (2026 H2 예정):
aegis install --target openclaw-local
```

미리 OpenClaw 자체를 익혀두고 싶다면:
- [openclaw.ai/docs](https://docs.openclaw.ai/) — 공식 문서
- [Telegram bot 데모](../integrations/openclaw.md#demo-scenario) — 5 시나리오 가이드

---

## 7. 자주 묻는 질문

**Q. 왜 Local OSS 가 cloud 보다 Aegis 활용도가 높나?**
A. 후크가 LLM 내부 메트릭에 직접 접근 가능하기 때문입니다. cloud LLM 은 `usage` 블록만 노출 — local 서버는 `/metrics` endpoint, GPU 메트릭, logit 등 모두 노출. 자세한 비교: [📊 LIVE_MANUAL.ko.md §3](../manuals/LIVE_MANUAL.ko.md) 와 본 트랙 §2.

**Q. 어떤 모델이 가장 적합한가?**
A. 코딩 어시스턴트로 쓸 거면 Llama 3.3 70B (Q4) 또는 Qwen 2.5 Coder 32B 가 권장. 가벼운 도구 호출만이면 Phi-3.5-mini (3.8B) 도 충분.

**Q. air-gapped 환경에서 model update 는?**
A. Aegis 가 `aegis pull-model --offline-bundle <path>` 로 사전 다운로드된 GGUF 를 import. update 는 `aegis baseline reattest` 로 새 모델 hash 를 baseline 갱신 후 진행.

**Q. 비용은?**
A. **이 트랙 자체는 무료** (Apache-2.0). 비용은 GPU + 전기. 일반적 시나리오 (RTX 4090 1대, 1일 8시간 사용) 기준 월 전기료 ~$15–25.

---

## 8. 다른 트랙

- [📋 릴리스 인덱스](README.md) — 세 트랙을 1 페이지로 비교
- [Claude Code](CLAUDE_CODE.ko.md) — 가장 간단한 시작점 (GA)
- [OpenClaw + Cloud LLM API](OPENCLAW_CLOUD.ko.md) — 다provider, 다채널 (Preview)
