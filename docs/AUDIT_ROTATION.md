# Audit log rotation 가이드

> Long-running Solo Free 사용자 (수개월 이상 사용) 의 disk fill 방지 +
> SHA3 chain 무결성을 rotation 경계 너머로 보존.

---

## 1. 왜 필요한가

PR #26 의 explain block 으로 audit 항목 평균 800 byte. 하루 1000 tool
call 가정 시:

| 기간 | audit.jsonl 크기 |
|---|---:|
| 1 일 | ~800 KB |
| 1 주 | ~5.6 MB |
| 1 달 | ~24 MB |
| 6 개월 | ~144 MB |
| 1 년 | ~290 MB |

PR #27 이전: 무한 추가 → 사용자 디스크 fill, `aegis verify-audit` 가
선형으로 느려짐.

PR #27 (이 PR): 50 MB 임계점 도달 시 rotation. 마지막 10개 보존.

---

## 2. 동작 원리

### 2.1 Rotation trigger

매 audit append 호출 시:
1. 활성 파일이 임계점 (`AEGIS_AUDIT_MAX_BYTES`, 기본 50 MB) 초과?
2. → rotation 실행:
   - oldest 파일 (`audit.jsonl.10`) 삭제 (보존 한도 초과)
   - 나머지 `.i` → `.{i+1}` 시프트
   - `audit.jsonl` → `audit.jsonl.1`
3. 새 `audit.jsonl` 에 새 record append

전체 과정 ms 미만 — 호크 hot path 영향 무시할 수준.

### 2.2 Chain 무결성 보존

핵심 도전: rotation 경계에서 SHA3 chain 끊지 않기.

기존 (회전 전):
```
audit.jsonl: [rec0]→[rec1]→[rec2]→[rec3]
            (genesis → h0 → h1 → h2 → h3)
```

회전 후 (잘못된 방식 — chain 끊김):
```
audit.jsonl.1: [rec0]→[rec1]→[rec2]→[rec3]
audit.jsonl:   [rec4]
              (genesis → h4)   ← 새 GENESIS 가 h3 와 안 이어짐
```

이 PR 의 방식 (chain 유지):
```
audit.jsonl.1: [rec0]→[rec1]→[rec2]→[rec3]
audit.jsonl:   [rec4]
              (h3 → h4)   ← rec4 의 prev_hash = h3 (rotated 파일의 last)
```

`_last_hash()` 가 활성 파일이 비어있을 때 자동으로 `audit.jsonl.1`
을 fallback 으로 사용 → 새 record 가 직전 rotation 의 last_hash 를
prev_hash 로 채택.

### 2.3 verify_chain 의 cross-file walk

`aegis verify-audit` (또는 `aegis audit verify`) 가 호출되면:

1. `list_rotation_chain()` 가 oldest → newest 순으로 모든 파일 enumerate
   - `audit.jsonl.10` (oldest) → ... → `audit.jsonl.1` → `audit.jsonl`
2. 각 파일 내에서 prev_hash → this_hash chain 검증
3. 파일 경계에서 last_hash 를 다음 파일의 expected_prev 로 전달
4. **Retention eviction 처리**: oldest 파일의 첫 record 의 prev_hash 가
   GENESIS_HASH 가 아닐 수 있음 (이미 evicted 된 파일 가리킴) — 그럴
   때 그 prev_hash 를 trust anchor 로 사용. "보존된 부분의 무결성"
   까지 검증 (전체 history 가 아닌).

---

## 3. CLI

```bash
# 현재 audit 파일 + rotation 목록 보기
uv run aegis audit list

  file                       size (KB)     records
  ──────────────────────────────────────────────────
  audit.jsonl.3                   2,156      12,300
  audit.jsonl.2                   2,156      12,500
  audit.jsonl.1                   2,156      12,400
  audit.jsonl                       845       5,100

# 수동 rotation (디버깅용 — 평소엔 자동)
uv run aegis audit rotate

# 전체 chain 무결성 검증
uv run aegis audit verify
✓ verify-audit (local chain) — 42,300 records intact
```

기존 `aegis verify-audit` 도 동일한 verify 함수 호출 — rotation 인식.

---

## 4. Configuration

| Env var | Default | 설명 |
|---|---|---|
| `AEGIS_AUDIT_MAX_BYTES` | 50 MB | rotation trigger 임계점. 0 = 비활성 |
| `AEGIS_AUDIT_MAX_ROTATIONS` | 10 | 보존 rotation 개수. 0 = 비활성 |
| `AEGIS_LOCAL_AUDIT` | `~/.aegis/audit.jsonl` | audit 파일 path |

비활성화 (legacy 동작 - unbounded growth):
```bash
echo "AEGIS_AUDIT_MAX_BYTES=0" >> .env
# 또는
echo "AEGIS_AUDIT_MAX_ROTATIONS=0" >> .env
```

---

## 5. dogfood `[14]` 검증

```bash
./scripts/dogfood_check.sh
```

```
[14] Audit rotation + verify
  ✓ rotated 4 files, 39 records, chain verified across boundaries
✓ 13/13 checks passed — green-light for real Claude Code
```

50 records 작성 → 4 files 회전 → 39 record 보존 (몇 개 evicted)
→ chain 검증 OK.

---

## 6. 한계

### 6.1 파일 시스템 atomic 보장 한계

`rotation` 은 `Path.replace()` (POSIX rename) 를 사용 — 같은 파일
시스템 내 atomic. 하지만 rotation 도중 (예: 1번 file 이름 바꾸고 2번
이름 바꾸기 전) 시스템 crash 시 일부 파일이 잘못된 슬롯에 남을 수
있음. 다음 append 호출은 정상 동작 (새 audit.jsonl 생성) 하지만
chain 의 earlier 부분에 gap 생길 수 있음.

→ 이 시나리오는 v1 에서 manually fix 필요. v2 에서 atomic group
rename 추가 검토.

### 6.2 Disk space spike

50 MB threshold 라도 instant 에 50 MB → 100 MB 까지 임계 직전 1 record
가 jump 가능 (audit append 가 atomic). 보통 rotation 후 실제 size
~50 MB 근처.

10 rotations × 50 MB = 최대 500 MB disk 사용. 사용자가 더 작은 footprint
원하면 `AEGIS_AUDIT_MAX_ROTATIONS` 를 낮춰야 함.

### 6.3 첫 retained record 의 prev_hash 검증 안 됨

eviction 후엔 oldest retained record 의 prev_hash 가 evicted file 을
가리킴. 그 link 자체는 검증 불가 (deleted file 이라). 이 PR 은 그
prev_hash 를 trust anchor 로 사용 — "이후 record 가 무결성 유지" 보장.

→ "전체 history 가 evicted record 까지 무결성" 보장은 backup 으로 대체
(rotation 파일을 cold storage 로 archive).

---

## 7. 누적 9개 PR

```
PR #20  install plumbing
PR #21  Llama-3.2-1B sLLM judge
PR #22  BGE-base-en embedding
PR #23  M13 v2 weights 학습
PR #24  step340 RAG (case memory)
PR #25  session_behavioral_drift
PR #26  aegis report --explain
PR #27  Phi-3.5-mini upgrade path + Metal
PR #28  audit log rotation + cross-file verify
```

dogfood: **13/13 PASS.** 회귀: **7/7 PASS.** 1394 tests.

운영 hygiene 완성. Long-running Solo Free 사용자가 수개월 사용해도
disk fill 없이, audit chain 무결성도 유지됨.

---

## 8. 다음 트랙 후보

| Track | 효과 |
|---|---|
| T2 sidecar (Docker daemon) | Phi-3.5 cold load 제거 |
| `aegis uninstall` | UX (settings.json cleanup) |
| `aegis report --explain --json` | CI / 자동화 |
| Shadow → M13 v3 retrain pipeline 검증 | patent value (한 달 후) |
