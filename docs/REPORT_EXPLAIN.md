# `aegis report --explain` 가이드

> "왜 BLOCK 됐어?" 한 줄 reason 만 봐서는 부족할 때, 결정에 기여한
> **모든 신호 layer** 를 풀어서 보여주는 explain 모드.

---

## 1. 한 줄 사용법

```bash
uv run aegis report --explain LAST           # 가장 최근 결정 explain
uv run aegis report --explain abc12345        # trace_id prefix 로 특정 결정
```

---

## 2. 출력 예시

```
AegisData Decision Explanation  ⛔
═══════════════════════════════════════════════════════════════════
  trace:     cad70ac33ecaa7b8…
  decision:  BLOCK
  tool:      Bash
  aid:       sess-explain
  latency:   2086.266 ms
  reason:    dangerous pattern: \brm\s+-rf\s+/

  Firewall steps (non-trivial):
    step305_safe_allowlist        step305: not safe-listed
    step308_identity              step308: skipped (no proof, require=false)
    step309_instruction_drift     step309: baseline disabled
    step310_args                  step310: static pattern hit (\brm\s+-rf\s+/)

  M13 attribution top contributors:  (combined score = 0.5584)
    tool_arg_inspection               [██████··············]  0.300
    action_blast_radius               [█████···············]  0.250
    action_history                    [····················]  0.008
    agent_state_embedding             [····················]  0.000
    inter_agent_graph                 [····················]  0.000

  step340 RAG (3 retrieved):
    top cos:    0.625
    top label:  ALLOW
    top case:   user wants to inspect the codebase  | tool=Bash …

  Session behavioural drift:
    current topic_drift: 0.000  max so far: 0.000  (call 1 of session)

  ATV: 2080-D, SHA3 = 6f09775f957d73df80a9d657…
```

---

## 3. 보여주는 정보

### 3.1 헤더
- `trace`: audit chain 의 trace_id (앞 16자)
- `decision`: ALLOW / BLOCK / REQUIRE_APPROVAL
- `tool`: 호출된 Claude Code tool 이름
- `aid`: agent identity (Solo Free 에서는 session_id)
- `latency`: firewall 통과 시간 (cold call ~2s, warm ~50ms)
- `reason`: 결정 사유 한 줄

### 3.2 Firewall steps (non-trivial)
13-step 파이프라인 중 **의미 있는 출력** 을 낸 step 만 (필터링됨 —
"ok" / "skipped" 같은 trivial entry 는 제외). 각 step 이 어떤 신호를
관찰했는지 보여줌.

특히 BLOCK 결정의 경우 어느 step 이 trigger 했는지 정확히 봄:
- `step310_args`: regex / 패턴 매치
- `step308_identity`: agent identity proof 검증
- `step309_instruction_drift`: CLAUDE.md / AGENTS.md drift
- `step336`: loop 탐지
- `step340`: sLLM judge

### 3.3 M13 attribution top contributors
M13 attribution head 의 30 subfield 점수 중 top-5. 각 subfield 의
contribution = `weight × base_value`. 합산이 `m13_score`.

Bar chart 로 시각화해서 어느 신호가 dominant 인지 한눈에:
- `[██████···········]` 0.300 → 30% (tool_arg_inspection 강함)
- `[····················]` 0.000 → 신호 없음

### 3.4 step340 RAG (BGE 활성 시)
가장 비슷한 과거 사례 1개의:
- `top cos`: 코사인 유사도
- `top label`: 그 사례의 verdict (ALLOW/BLOCK/REQUIRE_APPROVAL)
- `top case`: 사례 텍스트 일부

→ "이번 결정과 비슷한 과거 사례는 어땠는가?" 한 줄로 답.

### 3.5 Session behavioural drift (BGE + session_id 활성 시)
- `current topic_drift`: 이번 호출이 세션 시작 시점과 의미가 얼마나
  멀어졌는지 (0 ~ 2)
- `max so far`: 세션 내 최대 drift
- `n_calls`: 세션 내 몇 번째 호출인지

slow-drift attack 추적용.

### 3.6 ATV fingerprint
- `atv_dim`: 2080 (SW + HW band 합)
- `atv_sha3`: 결정에 사용된 ATV vector 의 SHA3-256

`aegis verify-audit` 와 결합해서 **decisive replay** 가능 — 같은
ATV 로 같은 firewall 돌리면 같은 verdict 나오는지 검증.

---

## 4. 사용 시나리오

### 4.1 BLOCK 됐는데 의도한 동작이었음 (false positive)

사용자가 `rm -rf /tmp/old_logs` 실행하려고 했는데 BLOCK:

```bash
$ uv run aegis report --explain LAST
```

→ `step310_args: static pattern hit (\brm\s+-rf\s+/)` 확인.
→ 결정 layer 가 명확. `policies/safe_actions.json` 에 추가하면
  step305 가 우회시킴.

### 4.2 ALLOW 됐는데 의심스러움

```bash
$ uv run aegis report --explain LAST
```

→ M13 attribution 다 0.0, RAG 사례도 cos < 0.4 → 정말 안전한 패턴.
→ 또는 `m13_score=0.45` (REQUIRE_APPROVAL threshold 미만) — borderline
  이었음을 알 수 있음.

### 4.3 시나리오 비교 분석

여러 결정을 차례로 explain 해서 패턴 분석:

```bash
$ tail -10 ~/.aegis/audit.jsonl | jq -r '.trace_id' | head -3 | \
    while read t; do uv run aegis report --explain "$t"; done
```

---

## 5. Audit-chain 통합

PR #26 이전에 기록된 audit 항목은 explain block 이 없음 →
"no explain block — record predates" 안내.

PR #26 이후의 모든 PreToolUse 결정에 explain block 자동 부착. 추가
스토리지 비용: 1 record 당 ~500-800 bytes (M13 + RAG + drift +
fingerprint). 하루 1000 calls 기준 ~700 KB 추가.

---

## 6. 누적 7개 PR 의 Solo Free 통합 explainability

```
PR #20  install plumbing
PR #21  진짜 sLLM judge — Llama-3.2-1B Q4
PR #22  진짜 embedding — BGE-base-en-v1.5
PR #23  M13 v2 weights 학습 — Burn-in Shadow + synthetic seed
PR #24  step340 RAG — case memory + nearest-neighbour retrieval
PR #25  session_behavioral_drift — slow-drift attack 탐지
PR #26  aegis report --explain — 모든 신호 통합 explainability
```

이전 PR 들이 *생성한* 신호들을 사용자가 사후에 검토 가능. 한 번의
명령으로 결정의 모든 layer 가 가시화됨.

dogfood: **12/12 PASS.** 회귀: **7/7 PASS.** 1369 tests.

---

## 7. 다음 트랙 후보

| Track | 효과 | 메모 |
|---|---|---|
| Phi-3.5-mini 자동 swap | 정확도 (1B → 2.2B) | RAG follow 능력 향상 |
| Audit log rotation | 운영 (long-running 사용자) | size-based 회전 |
| Shadow → M13 v3 retrain pipeline 검증 | patent value | 한 달 수집 후 |
| `aegis uninstall` | UX cleanup | settings.json 자동 제거 |
| `aegis report --explain --json` | CI / 자동화 | 구조화된 출력 |
