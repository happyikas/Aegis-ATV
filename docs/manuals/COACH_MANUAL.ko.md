# 🏋️ ATV Coach 사용자 매뉴얼

> **ATV Coach** — Aegis가 당신의 환경 (개인/기업) 에 맞는 최적의 판정 파라미터를
> 학습해서, 모든 도구 호출 평가에 더 정확한 컨텍스트를 제공하도록 만드는 기능.
>
> 문서 버전: v0.1.0 · Aegis ATV personal MVP · 한국어 (정본)

---

## 1. ATV Coach 가 하는 일

Aegis 의 firewall 은 31 개의 룰 + sLLM judge + 30-subfield ATV-2080-v1 벡터로
각 도구 호출을 평가합니다. 그러나 *어떤 호출이 평소대로인지* 는 사용자마다
다릅니다 — 데이터엔지니어는 매일 `psql` 에 destructive 한 SQL 을 던질 수도
있고, 보안 엔지니어는 매주 `kubectl delete` 가 일상일 수 있습니다.

ATV Coach 는 **shadow 모드로 가만히 관찰만 하면서** 당신의 환경에서 어떤
호출이 정상이고 어떤 호출이 이상인지를 5-layer × 4-phase 로 학습한 다음,
sLLM judge 와 RAG 단계 (step340) 에 그 학습 결과를 주입합니다.

핵심 기능:

| 기능 | 무엇을 학습하는가 |
|------|-------------------|
| **Burn-in (M11)** | 5-layer 별 정상 분포 + per-layer phase 전이 (Observation → Shadow → Assisted → Production) |
| **Case Memory (step340)** | 과거 BLOCK / APPROVE 판정 케이스를 임베딩 벡터로 RAG 인덱싱 → 비슷한 신규 호출에 즉시 매칭 |
| **Advisor Calibration (M13)** | 8-advisor pipeline 의 가중치 / 임계값을 분석 → 추천 |
| **Cost Replay** | 과거 N일 치 audit log 위에 새 정책 / 임계값을 재실행 → "이 정책이라면 결과가 어떻게 달라졌을까" |

---

## 2. 빠른 시작 — 5분만에 학습 켜기

```bash
# 1) Coach 의 burn-in shadow 모드를 켭니다 (현재 sLLM 판정에 영향 없음, 관찰만)
export AEGIS_BURNIN_SHADOW=1

# 2) Claude Code 를 평소처럼 사용합니다.
#    → 모든 PreToolUse 이벤트가 ~/.aegis/burnin_shadow.jsonl 에 기록됩니다.

# 3) 1~2 주 (또는 약 500 호출 이상) 누적 후, layer 별 단계를 확인:
aegis burnin shadow-status

# 4) 충분히 누적되었으면 그 데이터로 Coach 의 baseline 을 학습:
aegis burnin retrain

# 5) 학습 결과를 baseline 으로 export 해서 다른 머신에도 배포:
aegis burnin export-baseline > my-org-baseline.json
```

학습이 끝나면 `step340 case-memory` 가 자동으로 reload 되며, 이후 모든 호출
은 학습된 baseline 위에서 평가됩니다.

---

## 3. Burn-in 5-Layer × 4-Phase 모델

### 3.1 Layer

| Layer | 무엇을 보는가 | 학습 시간 |
|-------|---------------|-----------|
| L1 — Tool Frequency | 도구별 / 시간대별 호출 빈도 | ~24h |
| L2 — Argument Distribution | 각 도구의 인자 패턴 (path, command, regex 등) | 2~5 days |
| L3 — Multi-step Sequence | 같은 세션 내 도구 연쇄 패턴 | 5~10 days |
| L4 — Per-tenant Embedding | 사용자/프로젝트 별 의미 임베딩 분포 | 7~14 days |
| L5 — Long-tail Anomaly | 위 4개에서 잡히지 않은 long-tail 케이스 | 14 days+ |

### 3.2 Phase

각 Layer 는 독립적으로 4 단계를 거칩니다:

```
Observation  →  Shadow  →  Assisted  →  Production
   (관찰만)      (병행 판정,    (정식 판정에      (해당 layer 가
                 결과 비교)     일부 반영)        주판정자)
```

**`aegis burnin shadow-status`** 는 각 layer 가 지금 어느 phase 에 있는지를
보여줍니다. 다음과 같이 표시됩니다:

```
$ aegis burnin shadow-status
ATV Coach — burn-in layer status
─────────────────────────────────────
L1 tool-frequency       Production    samples=4,712  TPR=0.94 FPR=0.02
L2 argument-distribution Assisted     samples=3,201  TPR=0.88 FPR=0.04
L3 multi-step-sequence   Shadow       samples=  892
L4 per-tenant-embedding  Observation  samples=  214
L5 long-tail-anomaly     Observation  samples=   38
─────────────────────────────────────
Total shadow events: 9,057   shadow log: ~/.aegis/burnin_shadow.jsonl
```

phase 가 자동으로 graduate 되려면 `samples`, `TPR`, `FPR` 가 모두 임계값을
넘어야 합니다 (코드: `src/aegis/burnin/phases.py::can_graduate`).

수동으로 한 layer 를 다시 Observation 으로 되돌리려면:

```bash
aegis burnin revert --layer L3
```

---

## 4. Case Memory — 과거 판정 케이스로 신규 호출 빠르게 매칭

Coach 가 학습한 결과 중 가장 가시적인 부분은 **case memory** 입니다 —
firewall 의 step340 단계에서 동작합니다.

### 4.1 어떻게 동작하는가

각 PreToolUse 호출의 ATV-2080-v1 벡터를 임베딩한 다음, 과거 판정 케이스
임베딩과 cosine similarity 를 계산합니다. 매우 비슷한 케이스가 있고 그
케이스가 BLOCK 이었다면 → 신규 호출도 BLOCK 으로 빠르게 결정 (sLLM judge
호출 없이 < 5 ms).

기본 임베딩 provider 는 dummy (결정적 SHA3 기반). 실제 임베딩 모델을
쓰려면:

```bash
# 옵션 A: OpenAI
export AEGIS_EMBEDDING_PROVIDER=openai
export OPENAI_API_KEY=...

# 옵션 B: 로컬 sentence-transformers (외부 호출 0)
export AEGIS_EMBEDDING_PROVIDER=local
```

### 4.2 Case memory 를 처음 빌드하기

```bash
# 가지고 있는 audit log 전체에서 케이스를 추출 → 인덱스 빌드
aegis case-memory build --since 30d

# 인덱스 상태 확인
aegis case-memory status
```

### 4.3 다른 머신의 baseline 가져오기 (조직 단위 배포)

같은 조직의 동료가 export 한 baseline 을 import:

```bash
aegis case-memory import --file team-baseline.jsonl
```

이렇게 하면 **개인 머신 #1 에서 학습한 것 = 머신 #2, #3 에서도 즉시 적용**
됩니다. 회사 단위로 baseline 을 내려보낼 수 있는 구조입니다.

---

## 5. Advisor Calibration — 8-advisor 가중치 튜닝

`aegis advise` (Doctor 매뉴얼 참조) 가 출력하는 추천은 8 개 advisor 의 가중
합산 결과입니다. 각 advisor 의 가중치는 처음엔 균일하지만, **사용자가 추천을
얼마나 많이 무시 / 채택했는지** 를 학습해서 자동 보정할 수 있습니다.

```bash
# 지난 30일 advisor 출력 vs 실제 결과 분석
aegis advisor-calibration analyse --since 30d

# 결과 기반 추천 생성 (즉시 반영하지 않음)
aegis advisor-calibration recommend

# 추천을 실제 가중치로 적용
aegis advisor-calibration apply
```

`analyse` 출력 예시:

```
ATV Coach — advisor calibration (last 30d)
────────────────────────────────────────────
advisor          recall  precision  signal  recommend
cost              0.87     0.91     1,202   ↑ weight +5%
latency           0.73     0.95       847   ↑ weight +3%
reliability       0.91     0.88     1,510   ─ keep
security          0.94     0.79     2,031   ↓ weight -2% (false positives high)
compliance        0.45     0.92        91   ↓ weight -8% (low recall)
safety            0.88     0.94       912   ─ keep
efficiency        0.61     0.83       342   ↑ weight +6%
governance        0.52     0.88        77   ─ keep
────────────────────────────────────────────
```

---

## 6. Cost Replay — "이 정책이었으면 어떻게 됐을까?"

새로운 budget / loop / drift 임계값을 production 에 적용하기 전에, 과거
audit log 위에서 "만약 그 임계값이었다면 결과가 어떻게 달라졌을까" 를
시뮬레이션할 수 있습니다.

```bash
# 지난 7일 audit log 위에 새 임계값으로 재실행
aegis cost replay \
    --since 7d \
    --token-budget 50000 \
    --loop-threshold 5

# 출력: 추가 BLOCK / 새로 ALLOW 된 케이스 수, 비용 절감 추정치
```

이 결과를 보고 정책을 채택할지 결정한 다음:

```bash
# 채택
export AEGIS_TOKEN_BUDGET=50000
export AEGIS_LOOP_THRESHOLD=5
```

---

## 7. 환경 변수 레퍼런스

| 변수 | 의미 | 기본값 |
|------|------|--------|
| `AEGIS_BURNIN_SHADOW` | shadow 모드 활성화 (학습용 데이터 수집) | `0` |
| `AEGIS_SHADOW_LOG` | shadow 이벤트 로그 경로 | `~/.aegis/burnin_shadow.jsonl` |
| `AEGIS_EMBEDDING_PROVIDER` | case memory 임베딩 (`dummy`/`openai`/`local`) | `dummy` |
| `AEGIS_TOKEN_BUDGET` | per-session 토큰 예산 (step335 게이트) | unset (게이트 OFF) |
| `AEGIS_LOOP_THRESHOLD` | 같은 호출 N 회 이상 → REQUIRE_APPROVAL | `3` |
| `AEGIS_COACH_AUTOTRAIN` | shadow 충분히 쌓이면 자동으로 retrain | `0` |

---

## 8. 자주 묻는 질문

**Q. burn-in 학습 중 잘못 BLOCK 당하면?**
A. shadow phase 동안은 실제 판정에 영향이 없습니다 — Coach 는 기존 룰 +
sLLM judge 판정을 *기록만* 합니다. Assisted phase 부터 일부 반영되며,
이때 false positive 는 다음 retrain 에서 학습 데이터에 포함됩니다.

**Q. 회사 baseline 을 동료한테 어떻게 전달하나?**
A. `aegis burnin export-baseline > team.json` 으로 export, 동료 머신에서
`aegis case-memory import --file team.json`. baseline 은 raw 데이터가
아니라 **임베딩 + 룰 가중치 + phase 상태** — 원본 호출 인자가 들어있지
않습니다 (개인정보 leak risk 없음).

**Q. shadow 데이터 자체에 PII 가 들어갈까?**
A. shadow log 는 ATV-2080-v1 벡터의 30 개 subfield + reason 토큰만
저장합니다. 원본 명령어/파일 경로는 hash 처리. 자세한 schema:
[`src/aegis/schema.py`](../../src/aegis/schema.py).

**Q. 학습된 모델이 외부로 나가나?**
A. 0 — Coach 는 fully local. embedding provider 가 `openai` 일 때만 OpenAI
API 로 임베딩 호출이 나가며, 그것도 ATV 벡터의 텍스트 표현이지 원본이
아닙니다. 기본값 (`dummy` / `local`) 에서는 외부 호출 0.

---

## 9. 관련 문서

- [📊 LIVE_MANUAL.ko.md](LIVE_MANUAL.ko.md) — 학습된 결과를 실시간 모니터링
- [🔧 DOCTOR_MANUAL.ko.md](DOCTOR_MANUAL.ko.md) — 학습 결과를 진단·추천에 활용
- [PERSONAL_QUICKSTART.md](../PERSONAL_QUICKSTART.md) — 5분 설치 가이드
- [ARCHITECTURE.md](../ARCHITECTURE.md) — burn-in 내부 설계 (M11)
