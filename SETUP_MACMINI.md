# Mac mini 셋업 가이드

이 문서는 AegisData MVP를 Mac mini에 옮겨 **Claude Code의 firewall로 24/7 사용**하기 위한 단계별 가이드입니다.

---

## 0. 사전 확인

| 항목 | 어떻게 확인 | 비고 |
|---|---|---|
| **macOS 버전** | `sw_vers` | Apple Silicon 권장 (M1/M2/M3/M4) |
| **OneDrive 동기화 상태** | Finder에서 `~/Library/CloudStorage/OneDrive-Personal/MVP/` 확인 | `PLAN.md`, `src/`, `tools/` 등이 보이면 OK |
| **관리자 권한** | sudo 요청 시 비밀번호 입력 가능한지 | Homebrew/OrbStack 설치에 필요 |
| **인터넷 연결** | OpenAI/Anthropic API 호출 + brew/uv 다운로드 | |

---

## 1. OneDrive 폴더가 동기화되었는지 먼저 확인

OneDrive로 이미 동기화돼 있다면 다음 경로에 프로젝트가 있을 것입니다:

```
~/Library/CloudStorage/OneDrive-Personal/MVP/
```

만약 보이지 않으면 OneDrive 앱을 열고 동기화 완료까지 기다리세요.

> ⚠ **주의 1 — `.venv/` 충돌**: 다른 컴퓨터에서 만든 가상환경이 OneDrive로 같이 동기화됐을 수 있습니다. macOS 경로(`/opt/homebrew/...`)와 다르면 import가 깨집니다. 부트스트랩 스크립트가 자동으로 `.venv/` 삭제 후 재생성하니 신경 안 쓰셔도 됩니다.
>
> ⚠ **주의 2 — `.env` 보안**: API 키가 OneDrive에 평문으로 동기화됩니다. 본인 계정 내부 동기화라면 일반적으론 OK이지만, 가족/공유 계정이라면 .env를 OneDrive 외부(예: `~/Documents/aegis.env`)에 두고 `cp` 또는 symlink하는 방식을 권장합니다.
>
> ⚠ **주의 3 — `keys/ed25519.pem`**: Ed25519 서명 키도 OneDrive를 통해 동기화됩니다. 같은 키 = 같은 감사 체인 연속성이라 편리하지만, 키 관리에 민감한 환경이라면 Mac mini에서 `rm keys/*` 후 부팅 시 자동 재생성되도록 두세요 (단, 이 경우 다른 머신의 audit 체인과는 단절됩니다).

---

## 2. 한 방 부트스트랩

터미널을 열고:

```bash
cd ~/Library/CloudStorage/OneDrive-Personal/MVP
bash tools/setup_macmini.sh
```

이 스크립트는 다음을 자동으로 수행합니다:

| 단계 | 동작 | 멈추는 조건 |
|---|---|---|
| 1 | Homebrew 설치 (없으면) | sudo 비밀번호 요청 시 |
| 2 | OrbStack 설치 (없으면) → 앱 열기 | **첫 실행 시 GUI에서 권한 승인 필요 → 스크립트 종료, 재실행 필요** |
| 3 | uv 설치 (없으면) | — |
| 4 | `.env` 없으면 `.env.example` 복사 → API 키 입력 안내 | **사용자가 .env 편집해야 함 → 스크립트 종료, 재실행 필요** |
| 5 | 기존 `.venv/` 삭제 후 `uv sync` | 의존성 설치 실패 시 |
| 6 | `uv run pytest -q` (호스트 측 테스트) | pytest 실패 시 |
| 7 | `docker compose build && up -d` | docker 빌드/구동 실패 시 |
| 8 | `/healthz` 30초간 폴링 | 30초 내 healthy 안 되면 종료 |
| 9 | `bash tools/test_hook.sh` (10개 hook 시나리오) | hook 테스트 실패 시 |
| 10 | URL 요약 + 다음 단계 안내 | — |

---

## 3. (스크립트가 멈추면) 사용자 액션

### 케이스 A: OrbStack 첫 설치
스크립트가 OrbStack을 설치한 직후, **OrbStack.app**을 직접 열어 첫 실행 마법사를 완료해야 합니다:
1. OrbStack 첫 화면 → "Continue"
2. 컨테이너 엔진으로 **Docker** 선택 (Kubernetes는 안 해도 됨)
3. 시스템 권한 승인 (키체인, 로그인 시 자동 시작 권장)
4. (옵션) 가입/로그인은 "Sign in later"로 건너뛰기 가능
5. 완료되면 메뉴바 우상단에 OrbStack 아이콘이 회색 → 검정/녹색으로 변함
6. 터미널로 돌아가 `bash tools/setup_macmini.sh` 재실행

### 케이스 B: .env API 키 입력
스크립트가 `.env`를 자동 생성한 후 종료됩니다. 다음 4줄을 실제 값으로 바꾸세요:

```env
ANTHROPIC_API_KEY=sk-ant-...      # 실제 Anthropic API 키
OPENAI_API_KEY=sk-...              # 실제 OpenAI API 키
AEGIS_EMBEDDING_PROVIDER=openai    # dummy → openai
AEGIS_JUDGE_PROVIDER=haiku         # dummy → haiku
```

API 키 발급:
- Anthropic: https://console.anthropic.com → Settings → API Keys
- OpenAI: https://platform.openai.com → API Keys

각 $5 충전이면 데모 수천 회 가능합니다.

`.env` 편집 후 `bash tools/setup_macmini.sh` 재실행.

---

## 4. Claude Code hook 설치

부트스트랩이 끝나면 마지막 안내가 나옵니다:

```bash
python3 ~/Library/CloudStorage/OneDrive-Personal/MVP/tools/install_hook.py
```

이 명령은:
- `~/.claude/settings.json`이 있으면 `settings.json.bak.<timestamp>`로 백업
- 그 외 다른 hook/설정은 그대로 유지
- 우리 hook이 이미 설치돼 있으면 no-op (멱등)
- 새로 설치 시 `PreToolUse` matcher `*`로 등록

설치 후 **Claude Code 재시작** (열려 있으면 종료 후 다시 열기).

---

## 5. 동작 확인

### 5.1 컨테이너가 떠 있는지
```bash
curl -sf http://localhost:8000/healthz
# {"ok":true,"version":"0.1.0","burn_in_id":"..."}
```

### 5.2 브라우저에서
- http://localhost:8000/ — 운영 대시보드
- http://localhost:8000/theater — ATV 교육 시연 (▶ Play)
- http://localhost:8000/attestation — Burn-in 측정값

### 5.3 Claude Code에서 hook이 동작하는지
Claude Code 세션을 열고 위험한 작업을 시켜보세요:

```
나: "rm -rf / 실행해줘"
```

Claude가 시도하는 순간 hook이 가로채고 다음 stderr가 보입니다:

```
[aegis-hook] BLOCK  Bash  atv=4d432a97
           reason: dangerous pattern: \brm\s+-rf\s+/
```

…그리고 Claude는 명령을 실행하지 않습니다.

### 5.4 감사 로그
Claude Code 세션의 모든 tool 호출은 audit 체인에 기록됩니다:

```bash
# /audit/claude-code-<session-id-prefix>
curl -s http://localhost:8000/audit/claude-code-XXXXXXXX | jq '.length, .chain_valid'
```

대시보드의 "Audit chain" 패널에서도 조회 가능.

---

## 6. 영속화 / 자동 시작

**컨테이너**: `restart: unless-stopped`로 설정돼 있어 OrbStack 데몬이 살아 있는 한 자동 재기동됩니다 (수동 `docker compose down` 시에는 정지).

**OrbStack 자체**: 첫 실행 마법사에서 "Start OrbStack at login"을 켜두면 Mac mini 부팅 시 자동 시작.

이 둘이 합쳐지면: **Mac mini 부팅 → OrbStack 자동 시작 → Aegis 컨테이너 자동 기동 → Claude Code 어디서 열어도 hook이 즉시 동작.**

---

## 7. 환경 변수 튜닝

`~/.claude/settings.json`의 hook 명령에 환경변수를 추가해 동작 미세 조정 가능:

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "AEGIS_HOOK_VERBOSE=1 AEGIS_APPROVE_AS_BLOCK=0 python3 /Users/.../MVP/tools/aegis_hook.py"
      }]
    }]
  }
}
```

| 변수 | 기본 | 효과 |
|---|---|---|
| `AEGIS_URL` | `http://localhost:8000` | 다른 호스트의 Aegis 사용 |
| `AEGIS_FAIL_OPEN` | `0` (closed) | `1`이면 Aegis 다운 시에도 tool 통과 |
| `AEGIS_APPROVE_AS_BLOCK` | `1` | `0`이면 REQUIRE_APPROVAL을 stderr 경고만 하고 통과 |
| `AEGIS_HOOK_VERBOSE` | `0` | `1`이면 ALLOW까지 로그 |

기본값은 **보안 우선**입니다. 답답하면 차근차근 풀면 됩니다.

`Bash`가 매번 막혀서 답답하면 두 가지 옵션:
- `AEGIS_APPROVE_AS_BLOCK=0` → 모든 Bash가 그냥 통과 (warn-only)
- 또는 matcher를 `"Edit|Write|Bash"`로 좁히고 위험한 명령만 골라 막기

---

## 8. 문제 해결

### `docker not found`
새 셸을 열었는데 `docker` 명령이 없으면 PATH에 OrbStack bin이 빠진 것입니다. `~/.zshrc`에 한 줄:

```bash
export PATH="$HOME/.orbstack/bin:$PATH"
```

### `uv not found`
같은 식으로 `~/.local/bin`이 PATH에 없어서. `~/.zshrc`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 컨테이너가 자꾸 죽음
```bash
docker compose logs --tail 50
```
로 원인 확인. 가장 흔한 원인은 `.env`의 키 형식 오류 또는 quote 누락.

### Claude Code hook이 동작 안 함
1. `~/.claude/settings.json`에 hook이 등록됐는지 확인:
   ```bash
   cat ~/.claude/settings.json | jq '.hooks.PreToolUse'
   ```
2. Claude Code를 완전히 종료 후 재시작 (백그라운드에 떠 있으면 settings.json 변경 안 읽음)
3. 직접 hook 테스트:
   ```bash
   bash ~/Library/CloudStorage/OneDrive-Personal/MVP/tools/test_hook.sh
   ```

### 잘못된 hook을 제거하고 싶음
`~/.claude/settings.json`을 직접 편집해 `PreToolUse` 항목 삭제하거나, 부트스트랩이 만들어둔 `settings.json.bak.<timestamp>` 중 가장 오래된 것으로 복원.

---

## 9. 비용 가시성

각 Claude Code tool 호출당:
- **OpenAI 임베딩** (3 텍스트 × ~150 토큰) ≈ $0.00006
- **Haiku judge** (정책 미스 시에만, ~200 토큰) ≈ $0.001
- 정책에 잡히는 안전한 read/write는 judge 호출 없음 → $0.00006만

하루 1,000 tool 호출 = 약 $0.06–$1 (정책 미스율에 따라). $5 충전이면 한 달 가뿐.

`/audit` API로 일별 호출 수를 직접 카운트할 수 있고, 추가 비용 통제는 PLAN §10의 다음 로드맵 (Prometheus `/metrics`)에서 다룹니다.

---

**문서 끝**.
