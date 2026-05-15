# Aegis ATV — 사용자 매뉴얼 (한국어 정본)

> **처음 사용하시나요?** [`docs/USER_GUIDE.ko.md`](../USER_GUIDE.ko.md) 가 비전문가용 통합 가이드 — 5–10분 안에 "Aegis 가 무엇이고 어떻게 쓰는지" 한 페이지로. 이 디렉토리는 그 다음 단계의 깊은 reference 입니다.

---

Aegis ATV (Action Transparency & Verification) 의 사용자용 매뉴얼은 제품의
3개 기능 단위 (**Coach / Live / Doctor**) 로 구성되어 있습니다. 각 기능은
독립적으로 사용해도 되고, 셋이 조합되면 "agent 의 작업을 학습 → 모니터링
→ 진단 / 치료" 의 사이클을 만듭니다.

## 3개 기능

### 🏋️ [ATV Coach](COACH_MANUAL.ko.md)

당신의 환경에 맞는 정상 / 이상 분포를 **5-layer × 4-phase 로 학습** 해서
firewall 의 sLLM judge 와 RAG 단계 (step340) 에 주입합니다.

대표 명령:

```bash
export AEGIS_BURNIN_SHADOW=1   # shadow 학습 모드
aegis burnin shadow-status      # layer 별 학습 진행도
aegis burnin retrain            # 누적 데이터로 baseline 학습
aegis case-memory build         # RAG 인덱스 빌드
```

→ [COACH_MANUAL.ko.md](COACH_MANUAL.ko.md) 전체 보기

---

### 📊 [ATV Live](LIVE_MANUAL.ko.md)

agent 의 실행 현황을 **Cost / Performance / Security** 3 축으로 실시간
모니터링합니다.

대표 명령:

```bash
aegis report --since 24h          # 5 줄 요약
aegis cost summary --since 7d     # 비용 분석
aegis status --performance        # 성능 대시보드
aegis fleet-monitor start         # daemon + Slack/ntfy 알림
```

→ [LIVE_MANUAL.ko.md](LIVE_MANUAL.ko.md) 전체 보기

---

### 🔧 [ATV Doctor](DOCTOR_MANUAL.ko.md)

agent 가 사고를 쳤거나 칠 가능성이 높을 때 **forensic / advise / rollback**
으로 fix 하거나 advice 를 제공합니다.

대표 명령:

```bash
aegis forensic last               # 마지막 호출 timeline
aegis advise --since 24h          # 8-advisor 추천
aegis rollback <trace>            # 사고 호출 되돌리기
aegis health                      # Aegis 자체 헬스
```

→ [DOCTOR_MANUAL.ko.md](DOCTOR_MANUAL.ko.md) 전체 보기

---

## 슬래시 커맨드 (Claude Code 안에서)

설치 후 Claude Code 안에서 그대로 사용 가능:

| 슬래시 | 버킷 | 설명 |
|--------|------|------|
| `/aegis-report` | 📊 Live | 5 줄 위험 요약 |
| `/aegis-verify` | (Neutral) | audit chain 검증 |
| `/aegis-advise` | 🔧 Doctor | advisor 추천 |
| `/aegis-forensic` | 🔧 Doctor | 호출 timeline |
| `/aegis-help` | (Neutral) | 슬래시 메뉴 |

---

## 설치 / 운영 (Neutral 인프라)

3 개 버킷 어디에도 속하지 않는 공통 운영 명령:

| 명령 | 용도 |
|------|------|
| `aegis install` / `uninstall` | 후크 설치 / 제거 |
| `aegis audit-key init` / `show` | Ed25519 서명 키 (opt-in) |
| `aegis verify-audit` | audit chain 무결성 검증 |
| `aegis baseline diff` / `reattest` | instruction baseline (step309) |
| `aegis pull-model` | Solo Free 로컬 sLLM 다운로드 |

→ 5분 설치 가이드: [PERSONAL_QUICKSTART.md](../PERSONAL_QUICKSTART.md)

---

## 어떤 릴리스 트랙 매뉴얼을 봐야 할까

위 3개 기능 매뉴얼 (Coach / Live / Doctor) 은 **모든 릴리스 트랙에서 동일하게**
적용됩니다. 자기 환경에 맞는 트랙은 별도 가이드:

- [📋 docs/releases/](../releases/README.md) — 3개 트랙 1페이지 비교 + 결정 매트릭스
  - 🟢 [Claude Code](../releases/CLAUDE_CODE.ko.md) (GA)
  - 🟡 [OpenClaw + Cloud LLM API](../releases/OPENCLAW_CLOUD.ko.md) (Preview)
  - 🟡 [OpenClaw + Local OSS LLM](../releases/OPENCLAW_LOCAL.ko.md) (Preview)

---

## 영문 / 다른 언어

이 디렉터리의 매뉴얼은 **한국어 정본** 입니다. 영문 / 다른 언어 매뉴얼은
필요 시 `docs/manuals/COACH_MANUAL.en.md` 등으로 추가될 예정입니다.

이전 버전 (v2.x) 통합 매뉴얼은 [`docs/MANUAL_v2.2.md`](../MANUAL_v2.2.md)
를 참고하세요. v0.1.0 부터 위 3 분할 구조로 전환됩니다.

---

## 라이선스

Apache-2.0. 자세한 내용은 [`LICENSE`](../../LICENSE).
