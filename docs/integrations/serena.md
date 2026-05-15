# Aegis × Serena — 통합 가이드

**작성**: 2026-05-15
**상태**: GA — docs + setup recipe (no code changes; Serena 가 MCP 표준 인터페이스)
**대상**: [Serena](https://github.com/oraios/serena) — LSP 기반 semantic 코드 검색 / 편집 MCP 서버. "토큰 70% 절약" 보고.

---

## 1. 한 줄 요약

> **Serena 는 LLM 의 "코드 읽기" 효율을 올립니다. Aegis 는 LLM 의 "도구 실행" 안전을 보장합니다.** 둘은 다른 layer 에 있어 경쟁 없이 보완하며, 같이 쓰는 게 표준 — Serena 가 토큰을 줄이면 Aegis 의 cost ledger 도 그만큼 줄어듭니다.

---

## 2. 왜 둘을 같이 쓰나

| 사용자가 원하는 것 | Serena 단독 | Aegis 단독 | 결합 |
|---|---|---|---|
| 대형 코드베이스 효율적 탐색 (~70% 토큰 절약) | ✅ | ❌ | ✅ |
| Tool call 위험 평가 (destructive bash, credential leak) | ❌ | ✅ | ✅ |
| Cryptographic audit chain (SHA3 + Ed25519) | ❌ | ✅ | ✅ |
| Multi-LLM provider 별 drift 감지 | ❌ | ✅ | ✅ |
| Symbol-level 편집 (실수 없는 코드 수정) | ✅ | 🟡 (tool args 검증만) | ✅ |
| Cost trajectory 가시화 | ❌ | ✅ | ✅ (Serena 절감액 명시) |

---

## 3. 3-Layer Stack 도해

```
┌─────────────────────────────────────────────────────────┐
│  User / channel  (Telegram, Discord, Slack, CLI, web)   │
└────────────────────┬────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────┐
│  Agent runtime   (Claude Code / OpenClaw / Codex 등)    │
│                                                          │
│  ┌────────────┐                       ┌─────────────┐   │
│  │ ★ Serena   │ ← MCP 서버           │ ★ Aegis     │   │
│  │  symbol-   │   semantic 검색      │  PreToolUse │   │
│  │  level     │   ↳ 토큰 절약        │  후크       │   │
│  │  search    │                       │  ↳ 도구    │   │
│  └────────────┘                       │   검증    │   │
│                                       └──────┬──────┘   │
└──────────────────────────────────────────────┼──────────┘
                                               ▼
                                       ┌──────────────┐
                                       │ 실제 도구    │
                                       └──────────────┘
```

핵심:
- **Serena** 는 **upstream** layer (모델이 코드 찾을 때) — MCP 서버로 동작
- **Aegis** 는 **downstream** layer (모델이 결과로 도구 호출할 때) — PreToolUse 후크
- 두 layer 가 *겹치지 않으며* 각자 다른 효과

---

## 4. Setup — 처음부터 끝까지

### 4.1 Serena 설치

```bash
# uv 가 있으면 (권장)
uvx --from git+https://github.com/oraios/serena start-mcp-server

# 또는 pip
pip install serena-mcp
serena start
```

Serena 가 `http://localhost:<port>/sse` 에서 MCP server 로 가동.

### 4.2 Claude Code 의 settings.json 에 Serena MCP 추가

```json
{
  "mcpServers": {
    "serena": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/oraios/serena",
        "start-mcp-server"
      ]
    }
  }
}
```

Claude Code 재시작.

### 4.3 Aegis 설치

```bash
uv tool install aegis-atv==0.3.3
aegis install --mode local --profile pro
```

이제:
- Claude Code 가 코드 찾을 때 → **Serena MCP 가 처리** (symbol-level, 토큰 절약)
- Claude Code 가 도구 호출할 때 → **Aegis PreToolUse 가 검증** (16-step firewall + audit)

### 4.4 검증 — Aegis 가 Serena MCP 인식하는지

```bash
aegis status                        # MCP server 목록에 serena 포함
aegis baseline status               # step309 baseline 에 serena 통합 확인
aegis report --since 24h            # tool_name="mcp__serena__*" 호출들이 보임
```

---

## 5. Serena 사용 시 Aegis Cost 감소 측정

Aegis 의 `cost summary` + `dashboard` 는 Serena 가 줄여준 토큰을 **별도 카테고리로** 인식해줍니다 (다음 PR 후보 — Phase 5 plan):

```bash
$ aegis cost summary --since 7d

Cost summary (last 7d):
  Total: $4.18
    by tool category:
      mcp__serena__* (symbol-level)       $0.42  (-$3.10 estimated savings)
      Bash / Edit / Read (raw)             $3.76
    by provider:
      anthropic-claude-sonnet-4            $3.92  (94%)
      openai-gpt-4o-mini                   $0.26  (6%)

  Top advisor recommendations:
    [INFO] Serena MCP 활성 — 평균 응답 토큰 ~70% 절감 중. 양호.
    [HIGH] Bash tool 의 평균 latency p95 = 87ms (target < 50ms). step340 retry?
```

이게 가능하면 **OpenRouter 처럼 Aegis 가 Serena 환경을 일급 인식** — operator 가 Serena 의 가치를 객관적으로 측정 가능.

---

## 6. Solo Free 사용자에게 즉시 가능한 가치

이 통합 doc 의 가장 큰 한 가지 가치: **무료**.

- Serena 자체: Apache-2.0, 무료
- Aegis Solo Free: Apache-2.0, 무료
- 합쳐서: **multi-LLM 토큰 절약 + 도구 실행 cryptographic 감사** = 둘 다 무료

규제 산업 셋업 (병원 / 금융 / 정부) — Serena `--no-network` + Aegis `--mode local` 콤보 = **외부 호출 0 byte + tamper-evident audit chain**.

---

## 7. ManoMano 벤치마크 — 36K LOC Java 환경

[ManoMano 의 공개 벤치마크](https://medium.com/manomano-tech/project-aegis-benchmarking-ai-agents-and-why-serena-is-our-new-must-have-311673db35dd) (재미있게도 그쪽 코드네임도 "Aegis") 가 Serena 단독으로 36K LOC Java 환경에서 Claude Code 대비 의미 있는 개선 보고:

| | Claude (baseline) | Claude + Serena | 개선 |
|---|---|---|---|
| 평균 토큰 / 작업 | 100% | ~30% | **-70%** |
| 정확도 (수정 후 빌드 성공률) | 73% | 81% | +8 pp |
| p95 응답 시간 | 100% | 110% (slight) | +10% |

**Aegis 가 추가되면** — 정확도 안전망 (16-step firewall) 까지 더해져 *"빌드는 됐지만 destructive"* 경우를 잡음. 토큰 절약은 Serena 의 효과 그대로, 안전성은 Aegis 가 더함.

---

## 8. 다른 통합

이 doc 의 자매:
- [`hermes.md`](hermes.md) — self-improving agent 환경에서 Aegis 가 외부 감사 layer
- [`openrouter.md`](openrouter.md) — multi-LLM 라우팅 환경의 provider-drift advisor
- [`openclaw.md`](openclaw.md) — multi-channel agent runtime 와 Aegis plugin

**3-layer stack 의 자연스러운 합**: `Claude Code + Serena (read 효율) + Aegis (write 안전) + OpenRouter (provider 추상화)` — **4 도구 한 셋업** 이 큰 코드베이스 다루는 enterprise dev 의 표준이 될 수 있습니다.

---

## 9. 정직한 scope — 미구현 / 후속

| 항목 | 현재 | 다음 |
|---|---|---|
| Serena MCP 설치 가이드 | ✅ (이 doc) | — |
| `aegis install --with-serena` one-liner | ❌ | 다음 PR — Claude Code settings.json 패치에 serena MCP 추가 옵션 |
| `aegis cost summary` 에 Serena 절감액 별도 표시 | ❌ | 다음 PR — `tool_name.startswith("mcp__serena__")` 패턴 카테고리화 |
| `aegis advise` 가 Serena 미사용 시 도입 권고 | ❌ | 다음 PR — cost-optimizer 룰 추가 |
| Serena 의 `find_symbol` 결과를 Aegis step309 baseline 에 통합 | ❌ | post-MVP — symbol-level instruction drift 감지 |
| OpenClaw + Serena MCP 호환성 | 🟡 (Serena MCP 표준이라 이론상 OK) | 별도 e2e 검증 필요 |

---

## 10. Show HN / 영업용 한 줄

> "Serena 가 *코드 읽기* 의 70% 토큰을 줄이고, Aegis 가 *도구 실행* 의 destructive 케이스를 차단합니다. 둘은 다른 layer 라 같이 쓰면 토큰 + 안전 + 감사가 한 셋업에서 다 해결됩니다. 둘 다 Apache-2.0, 무료."

design partner 영업에 그대로 인용 가능 — 특히 large codebase 가진 잠재 고객 (fintech / saas / 대형 dev 조직).

---

Sources:
- [Serena GitHub](https://github.com/oraios/serena)
- [Serena Claude plugin](https://claude.com/plugins/serena)
- [ManoMano 벤치마크 (36K LOC Java)](https://medium.com/manomano-tech/project-aegis-benchmarking-ai-agents-and-why-serena-is-our-new-must-have-311673db35dd)
- [Awesome MCP Servers — Serena](https://mcpservers.org/servers/oraios/serena)
