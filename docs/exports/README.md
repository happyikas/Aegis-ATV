# 사용 설명서 — 배포용 형식

이 디렉토리는 [`docs/USER_GUIDE.ko.md`](../USER_GUIDE.ko.md) 의 **공유용 파일 형식** 입니다. 코드 / 한국어 본문이 같고, 다음 두 형식으로 제공합니다.

| 파일 | 용도 | 분량 |
|---|---|---|
| [`USER_GUIDE.ko.docx`](USER_GUIDE.ko.docx) | 이메일 첨부 / 인쇄 / 사내 회람 | ~26 KB · 14 섹션 + 목차 |
| [`USER_GUIDE.ko.pptx`](USER_GUIDE.ko.pptx) | 영업 미팅 · 데모 · 발표 | ~60 KB · 15 슬라이드 16:9 |

## 재생성 방법

원본 markdown 이 갱신되면 두 파일을 다시 생성:

### Word (.docx)

```bash
pandoc docs/USER_GUIDE.ko.md \
  --from gfm --to docx \
  --output docs/exports/USER_GUIDE.ko.docx \
  --toc --toc-depth=2 \
  --metadata title="Aegis ATV — 사용 설명서" \
  --metadata subtitle="비전문가용 통합 가이드" \
  --metadata author="AegisData" \
  --metadata date="2026-05-15"
```

### PowerPoint (.pptx)

```bash
uv run --with python-pptx python scripts/build_user_guide_pptx.py
```

`scripts/build_user_guide_pptx.py` 는 14 슬라이드를 **deterministic** 하게 생성합니다 — NVIDIA Inception PitchDeck 의 톤 (Midnight Executive 팔레트 + coral 강조 + monospace 코드 블록) 을 매칭. 디자인을 바꾸려면 스크립트 직접 편집.

## 슬라이드 구성 (PPTX)

| # | 제목 |
|---|---|
| 1 | 표지 — "AI 에이전트의 모든 행동을 실행 직전 검증" |
| 2 | 자물쇠 · CCTV · 영수증 비유 |
| 3 | "Below the model" chokepoint + 3 통계 (< 50ms / ≥ 90% / 1 cmd) |
| 4 | 5 사용자 페르소나 |
| 5 | 삭제 사고 시나리오 — Aegis 없을 때 vs 있을 때 |
| 6 | 4 가지 설치 옵션 |
| 7 | 첫 5 명령어 + 샘플 출력 |
| 8 | 3 기능 — Coach · Live · Doctor |
| 9 | PitchDeck 의 5 기술 — ATV · ATMU · sLLM · Crypto-Sign · Burn-in |
| 10 | 요금제 — Solo Free · Pro · Team · Enterprise |
| 11 | 통합 시나리오 — Claude Code · OpenClaw · OpenRouter · Hermes |
| 12 | FAQ 4 가지 |
| 13 | 자주 발생하는 문제 6 건 |
| 14 | **ContextMemory + aegis doctor** — CXL/CSD 매핑 (NEW) |
| 15 | 한 문장 요약 + install command |

## 디자인 결정 — 참고

| 요소 | 값 |
|---|---|
| 팔레트 | Navy `#1E2761` (dominant) · Ice `#CADCFC` (secondary) · Coral `#F96167` (accent) |
| 헤드 폰트 | Pretendard (fallback: Apple SD Gothic Neo · Malgun Gothic) |
| 본문 폰트 | Pretendard |
| 모노 폰트 | Consolas |
| 슬라이드 비율 | 16:9 widescreen (13.33 × 7.5 in) |
| 타이틀 | 32pt bold navy |
| 본문 | 13–16pt navy / dark ink |
| Eyebrow label | 11pt coral 대문자 |
| Divider | 0.6 inch coral accent bar (제목 아래) |

## 라이선스

본 가이드는 [Apache-2.0](../../LICENSE) — 원본 markdown + 두 export 파일 모두 자유롭게 재배포 가능합니다.
