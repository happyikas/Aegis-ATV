# Aegis ATV — Claude Code 트랙 (한국어 정본)

> **상태**: 🟢 **GA** (Personal MVP v0.1.0, Apache-2.0)
> **타겟**: Claude Code 를 일상적으로 쓰는 개인 / 솔로 개발자
> **설치**: `aegis install --target claude-code` (기본값)

---

## 1. 이 트랙이 누구를 위한 것인가

- 매일 Claude Code 를 쓰는 솔로 개발자
- Cloud LLM (Anthropic Claude) 사용에 거부감 없는 사용자
- 0 cloud calls (Solo Free contract) 가 필요한 사용자도 호환 — Aegis 자체는 fully local

이 트랙이 **default 이고**, README 의 "5분 설치" 안내는 모두 이 트랙 기준입니다.

---

## 2. 5분 설치

```bash
# 옵션 A: source clone (권장)
git clone https://github.com/happyikas/Aegis-ATV.git && cd Aegis-ATV
uv sync
uv run aegis install --target claude-code --mode local

# 옵션 B: Homebrew (macOS)
brew tap happyikas/aegis https://github.com/happyikas/Aegis-ATV.git
brew install happyikas/aegis/aegis
aegis install --target claude-code --mode local

# 옵션 C: pip (어디서나)
pip install aegis-atv
aegis install --target claude-code --mode local
```

설치 후 Claude Code 를 완전히 종료하고 재시작하면 firewall 이 활성화됩니다.

---

## 3. 무엇을 얻나

| 영역 | 무엇이 동작 |
|------|-------------|
| **🏋️ Coach** | burn-in 5-layer 학습 + case-memory RAG + advisor calibration |
| **📊 Live** | `aegis report`, `aegis cost summary`, `aegis fleet-monitor`, ATMU 트랜잭션 |
| **🔧 Doctor** | `aegis forensic`, `aegis advise`, `aegis rollback`, `aegis health` |
| **Slash 커맨드** | `/aegis-report`, `/aegis-verify`, `/aegis-advise`, `/aegis-forensic`, `/aegis-help` |
| **Audit chain** | SHA3 + 옵션 Ed25519 (`aegis audit-key init`) — `~/.aegis/audit.jsonl` |
| **16-step firewall** | step305 → step340 (전체 룰팩) |

자세한 사용법:
- [🏋️ COACH_MANUAL.ko.md](../manuals/COACH_MANUAL.ko.md)
- [📊 LIVE_MANUAL.ko.md](../manuals/LIVE_MANUAL.ko.md)
- [🔧 DOCTOR_MANUAL.ko.md](../manuals/DOCTOR_MANUAL.ko.md)

---

## 4. 이 트랙의 한계 (정직하게)

Claude Code 는 Anthropic 의 closed-source CLI 라 다음은 **이 트랙에서 영원히 못 봅니다**:

- Anthropic 서버측 KV cache 라우팅 결정
- Speculative decoding hit/miss
- per-token inference latency 분해
- GPU/HBM 사용률
- 모델 가중치 hash baseline (가중치 자체에 접근 불가)
- Logit-level forensic (왜 LLM 이 그렇게 답했나)

이 영역들이 필요하면 **OpenClaw + Local OSS** 트랙을 보세요 (향후 릴리스).

---

## 5. 이 트랙에서 *진짜 잘 작동하는 것*

위 한계의 반대로, 이 트랙이 가장 강한 영역:

1. **도구 호출 firewall** — 31 규칙 + sLLM judge 가 Claude Code 의 `--allowedTools` 보다 훨씬 정교함.
2. **Cryptographic audit chain** — 모든 도구 호출이 SHA3-chained, opt-in Ed25519 서명. `aegis verify-audit` 로 외부 검증 가능.
3. **Cost gate** (step335) — Anthropic 청구서 폭증을 *예측 시점* 에 차단.
4. **Instruction drift** (step309) — `CLAUDE.md` / `.mcp.json` / plugin manifest 가 몰래 바뀌면 자동 BLOCK.
5. **Loop detector** (step336) — 같은 도구가 N≥3 회 반복되면 REQUIRE_APPROVAL.

→ 이 5 개가 Aegis 의 **이 트랙에서의 핵심 가치 명제** 입니다.

---

## 6. 환경별 동작 모드

`aegis install --target claude-code` 는 두 모드 중 하나로 작동:

### 6-1. `--mode local` (기본 / 권장)

Claude Code 의 PreToolUse 후크가 자기 프로세스 안에서 firewall 을 직접 실행. 외부 서비스 없음.

```bash
aegis install --target claude-code --mode local --profile free
```

`--profile free` 면 외부 호출 0 (dummy embedding + dummy judge). `--profile pro` 또는 `--profile cloud` 면 advisor pipeline 활성화.

### 6-2. `--mode sidecar`

FastAPI 서비스를 별도 컨테이너로 실행 (`docker compose up -d`), Claude Code 후크가 `localhost:8000/evaluate` 로 POST. ATMU + cost ledger + Ed25519 서명 키 전체 surface 사용 가능. 멀티 사용자 환경 / sidecar API 가 필요할 때.

```bash
aegis install --target claude-code --mode sidecar
docker compose up -d
```

---

## 7. 자주 묻는 질문

**Q. Claude Code 자체의 `--allowedTools` 와 함께 써도 되나?**
A. 네 — Aegis 는 그 *위에* 얹는 layer 입니다. Claude Code 의 binary 허용/거부와 Aegis 의 16-step + audit chain 이 같이 동작합니다.

**Q. Anthropic API 키 없으면 동작하나?**
A. 네. `--profile free` (기본) 면 dummy embedding + dummy judge 로 fully local. Claude Code 자체에는 API 키가 필요하지만 Aegis 는 별개.

**Q. 다른 LLM (GPT, Llama) 도 지원하나?**
A. Claude Code 는 Anthropic 전용. 다른 LLM 을 쓰려면 [**OpenClaw + Cloud LLM**](OPENCLAW_CLOUD.ko.md) 또는 [**OpenClaw + Local OSS**](OPENCLAW_LOCAL.ko.md) 트랙을 기다리세요.

**Q. 회사 컴플라이언스 팀이 audit log 를 요구하면?**
A. `aegis audit-key init` 으로 Ed25519 서명을 켜고, `aegis verify-audit --strict` 출력 + public key (`~/.aegis/keys/audit.ed25519.pub`) 를 함께 넘기면 외부에서 검증 가능. 자세한 시나리오: [🔧 DOCTOR_MANUAL.ko.md §7.3](../manuals/DOCTOR_MANUAL.ko.md).

---

## 8. 다른 트랙

- [📋 릴리스 인덱스](README.md) — 세 트랙을 1 페이지로 비교
- [OpenClaw + Local OSS LLM](OPENCLAW_LOCAL.ko.md) — air-gapped, 가장 깊은 instrumentation (Preview)
- [OpenClaw + Cloud LLM API](OPENCLAW_CLOUD.ko.md) — 다채널, 다provider (Preview)
