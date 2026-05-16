# M13 v2 Weight 학습 가이드

> Patent Burn-in Shadow phase 의 1차 구현. 합성 데이터 → v2 weights →
> 실제 production 데이터 (Shadow log) 가 모이면 더 큰 자동 retrain 의
> scaffold.
>
> v0.5+: 아래 모든 `aegis burnin <action>` 명령은 새 vocab
> `aegis coach burnin <action>` 와 동일하게 동작합니다 (이전 이름은
> alias 로 유지).

---

## 1. 왜 필요한가

M13 attribution head v1 weights 는 **hand-tuned** — 실 데이터가 없던
v2.x 시절 사람이 눈대중으로 정한 30개 float. 한계:

- 학습된 게 아니라 직관 → 어떤 attack 패턴은 잡고 어떤 건 놓침
- 새 attack class (예: network exfil 의 새 변형) 가 나올 때마다 사람이
  weights 만지러 들어가야 함
- 7 시나리오 회귀 외엔 검증 metric 없음

v2 는 **데이터로부터** 학습. 같은 frozen-linear-classifier 아키텍처
(Claim 8 보존) 에서 30개 weights + 2개 threshold 만 재학습. 추론
경로는 v1 과 bit-identical.

---

## 2. 한 줄 학습

```bash
uv run aegis burnin train-m13
```

생성물: `models/m13_attribution_head_v2.json`. v1 은 그대로 두고 새
파일을 따로 만듦 (audit replay 호환성).

기본 동작: 245 개 합성 샘플 (7 카테고리 × 35) 에서 80/20 split, NNLS
projected gradient 로 학습, threshold grid-search 로 calibrate. ~15초.

---

## 3. v1 vs v2 비교

```bash
uv run aegis burnin compare-m13
```

출력:

```
  Metric              v1          v2          Δ
────────────────────────────────────────────────────────────
  3-class accuracy    0.245       0.539       +0.294
  False negatives     92          2           -90
  False positives     3           27          +24
  Asym cost (5×FN+FP) 463.0       37.0        -426.0

  Winner: V2  🅱️
```

**Asymmetric cost** = 5 × false_negatives + 1 × false_positives.

이유: Solo Free 의 **risk profile** 은 비대칭:
- False negative (악성 → ALLOW): 보안 사고. 5× 가중치
- False positive (정상 → BLOCK): 사용자 짜증. 1× 가중치

v2 가 false negative 를 92→2 (45× 감소) 한 대신 false positive 가
3→27 늘었음. Asym cost 로는 **463 → 37 (12.5× 개선)** — Solo Free
사용자 측면에서 명확히 v2 가 우수.

---

## 4. v2 채택

option A — v2 를 "공식" v1 으로 승격:

```bash
cp models/m13_attribution_head_v1.json models/m13_attribution_head_v1.json.bak
mv models/m13_attribution_head_v2.json models/m13_attribution_head_v1.json
./scripts/macmini_user_test.sh --hybrid    # 7/7 PASS 확인
```

option B — v1 / v2 둘 다 유지, 명시적 선택:

```python
from pathlib import Path
from aegis.judge.attribution_head import AttributionHead
head_v2 = AttributionHead(weights_path=Path("models/m13_attribution_head_v2.json"))
```

이 PR 은 option B 형태로 머지 — v1 디폴트 유지, v2 는 sample 로 동봉.
사용자가 confidence 가 생기면 option A 로 승격.

---

## 5. 진짜 production 데이터로 학습 (Burn-in Shadow phase)

합성 데이터의 한계:
- 카테고리 6개 (benign_read / destructive_bash / credential_leak /
  database_mutation / sensitive_path / cloud_destructive / network_exfil)
- 작성자가 상상한 패턴만 커버
- 실제 사용자의 코딩 워크플로우 분포와 다를 수 있음

**Burn-in Shadow phase** 는 hook 을 record-only 모드로 켜서 실제
traffic 의 (ATV, would-be verdict) pair 를 모음. 한 달 정도 모이면
v2 → v3 로 retrain:

```bash
# 1. Shadow 모드 활성화 (.env 또는 환경 변수)
export AEGIS_BURNIN_SHADOW=1

# 2. Claude Code 재시작 → 평소처럼 사용 → ~/.aegis/shadow.jsonl 에 기록
# 3. 진행 상황 확인
uv run aegis burnin shadow-status

# 4. 충분히 모였으면 학습
uv run aegis burnin train-m13 --corpus ~/.aegis/shadow.jsonl --out models/m13_v3.json

# 5. 평가
uv run aegis burnin compare-m13 \
  --v1 models/m13_attribution_head_v1.json \
  --v2 models/m13_v3.json
```

Shadow 모드는 **opt-in only** (env var 안 켜면 no-op). 기록 내용:
tool name + args + state text + verdict (transcript 본문 / 모델 출력
은 건드리지 않음 — step340 가 이미 audit 하는 surface 와 동일).

---

## 6. 학습 detail (꼼꼼한 사람을 위해)

### Architecture

v1 / v2 둘 다 identical:

```
score = Σ subfield_weight[i] × base[i]    (i = 0..29)

where base[i] = max(
    aggregator(slice_i),                # max(|slice|), clipped to [0,1]
    named_slot_score(slice_i, slot_weights[i]),  # encoder feature lookup
)

decision = BLOCK            if score ≥ threshold_block
         = REQUIRE_APPROVAL if score ≥ threshold_approval
         = ALLOW            otherwise
```

학습 대상: 30개 `subfield_weight` + 2개 threshold. `named_slot_weights`
와 aggregator 는 v1 그대로 보존 (encoder side).

### Optimizer: weighted projected-gradient NNLS

손실:

```
L(β) = Σ_i  w_i (X_i · β − y_i)²  +  λ ‖β‖²       s.t. β ≥ 0
```

- `X_i` = 30-D feature vector (i 번째 학습 예제의 base values)
- `y_i` = regression target (ALLOW=0.20, REQUIRE_APPROVAL=0.62, BLOCK=0.85)
- `w_i` = inverse class frequency (3-class 불균형 보정)
- `λ` = 1e-5 (작게 — Hessian 안정화 목적, signal weight 죽이지 않음)
- non-negativity: 매 step `β := max(β, 0)`

Update rule: `β := β − η ∇L(β)`, η = 1 / (2·λ_max(X^T W X) + 2λ).
Spectral-radius 기반 step size — 항상 contractive, manual tune 불필요.

20,000 iterations, ~1초 (210 examples × 30 features 기준).

### Threshold calibration

학습 후 grid-search:
- approval ∈ {0.30, 0.325, ..., 0.90}
- block ∈ {0.30, 0.325, ..., 0.90}, block > approval
- 3-class accuracy 최대화

### Determinism

같은 corpus → bit-identical weights:
- corpus 는 seed 기반 (`generate(seed=2026_05_03)`)
- NNLS solver 는 random init 이지만 수렴값은 unique (convex)
- threshold search 는 deterministic grid

`aegis verify-audit` 시 필요한 model_hash = `SHA3-256(v2 JSON file bytes)`.

---

## 7. 알려진 한계 / 다음 트랙

| 한계 | 미래 트랙 |
|---|---|
| 합성 데이터 의존 | Shadow phase real data 수집 |
| `agent_state_embedding` weight = 0 (BGE 활성 후에도) | M13 v3: BGE centroid distance feature 추가 |
| REQUIRE_APPROVAL 분리 어려움 (BLOCK 과 encoder feature 유사) | step340 RAG: 유사 BLOCK 사례 prompt 주입 |
| Logistic regression 안 씀 | v3 후보 — 3-class softmax 또는 ordinal regression |

---

## 8. 결론

이 PR 의 deliverable:

| 인프라 | 상태 |
|---|:---:|
| 합성 corpus generator (245 examples × 7 categories) | ✅ |
| NNLS trainer (frozen linear, Claim 8 호환) | ✅ |
| `aegis burnin train-m13` CLI | ✅ |
| v1 vs v2 evaluation harness + asymmetric cost | ✅ |
| `aegis burnin compare-m13` | ✅ |
| Burn-in Shadow record-only 모드 | ✅ opt-in |
| `aegis burnin shadow-status` | ✅ |
| 7 시나리오 회귀 (v2 default 로 PASS) | ✅ 7/7 |

**Patent value:** Solo Free 사용자가 처음으로 *데이터-기반* M13 weight
를 받음. 더 중요한 건 인프라가 있어서 이제 사용자가 자기 traffic 으로
M13 v3 로 retrain 가능 — Shadow phase 의 진짜 value 가 시작됨.
