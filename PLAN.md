# AegisData MVP — Claude Code 기반 T2 구현 계획

**문서 목적**: 이 문서는 Claude Code에 바로 넘겨 MVP 코드베이스를 구축할 수 있도록 작성된 설계·구현 가이드입니다. 특허 v4의 T2 티어(소프트웨어 전용)의 핵심 기능을 7일 내 동작하는 서비스로 만드는 것이 목표입니다.

**사용법**: 이 문서를 프로젝트 루트에 `PLAN.md`로 배치하고, Claude Code에서 `claude "PLAN.md 를 읽고 Section 9의 Milestone 1부터 시작해서 코드를 작성해줘"` 식으로 단계별 호출합니다.

---

## 0. 한 줄 요약

**"Claude/GPT/Gemini API 호출을 감싸, 모든 툴 호출 순간을 2,080차원 ATV로 서명·감사하는 Python 사이드카 서비스."**

메인 에이전트 로직(프롬프트, 모델 선택)은 그대로 두고, 툴 호출 직전·직후에 AegisData 미들웨어가 끼어듭니다. FastAPI로 HTTP/gRPC 인터페이스를 제공해 어떤 언어의 에이전트도 사용할 수 있습니다.

---

## 1. MVP 목표와 비-목표

### 1.1 목표 (In Scope)

1. 2,080차원 ATV 생성 (소프트웨어 밴드만 완전, 하드웨어 밴드 200-D는 zero-fill)
2. Action Firewall 7단계 전부 동작 (단 340의 정책 엔진은 간단한 JSON 룰로 MVP)
3. sLLM Judgment: Claude Haiku 4.5 API + 로컬 Phi-4-mini fallback (옵셔널)
4. Ed25519 서명 + Merkle-chained 감사 로그 (SQLite + append-only JSONL)
5. FastAPI 기반 REST 엔드포인트 3개: `/evaluate`, `/approve`, `/audit`
6. 데모: Claude Sonnet 4.6을 메인 에이전트로 쓰는 예제 스크립트
7. Docker Compose로 로컬 전체 스택 기동
8. Pytest 기반 단위 테스트 + 엔드 투 엔드 테스트

### 1.2 비-목표 (Out of Scope)

- 실제 TEE 배포 (Nitro/Confidential VM) — 로컬 Docker로 검증만
- Five-Layer Burn-in의 L1 (하드웨어 EK) — L3~L5만 소프트웨어로 에뮬레이트
- 포스트퀀텀 서명 (ML-DSA) — Ed25519만
- CSD 통합 (T3 티어 전체)
- In-Storage Similarity Engine
- Web UI — CLI와 API만

### 1.3 완성 기준 (Definition of Done)

- `docker compose up` → 헬스체크 녹색
- `python demo/agent_demo.py` → Claude가 5개 툴 호출하는 워크플로우 중 2개가 ALLOW, 1개가 BLOCK, 2개가 REQUIRE-APPROVAL 판정됨
- `pytest` 모든 테스트 통과, 커버리지 ≥ 70%
- `GET /audit?aid=agent-demo` → JSON 체인의 prev_hash 링크가 깨지지 않음이 검증됨

---

## 2. 아키텍처

```
                      ┌──────────────────────────────────┐
                      │     Demo Agent (Python script)    │
                      │     - Claude Sonnet 4.6 API       │
                      │     - 5개 툴 정의                 │
                      └───────────────┬──────────────────┘
                                      │
              HTTP POST /evaluate    │
              {atv_input, tool_call} │
                                      ▼
         ┌────────────────────────────────────────────────┐
         │         FastAPI Service (aegis_core)          │
         │                                                │
         │   ┌─────────────┐     ┌──────────────────┐   │
         │   │ ATV Builder │────▶│ Action Firewall  │   │
         │   │  - embed    │     │  Step 310-370    │   │
         │   │  - pack     │     └──────┬───────────┘   │
         │   └─────────────┘            │               │
         │                              ▼               │
         │                       ┌──────────────┐       │
         │                       │ sLLM Judge   │       │
         │                       │ (Haiku 4.5)  │       │
         │                       └──────┬───────┘       │
         │                              ▼               │
         │                       ┌──────────────┐       │
         │                       │ Ed25519 Sign │       │
         │                       └──────┬───────┘       │
         │                              ▼               │
         │                       ┌──────────────┐       │
         │                       │ Audit Log DB │       │
         │                       │ (SQLite +    │       │
         │                       │  JSONL WORM) │       │
         │                       └──────────────┘       │
         └────────────────────────────────────────────────┘
                                      │
                                      ▼
                           verdict: ALLOW / BLOCK / APPROVAL
                           + atv_id + signature
```

---

## 3. 기술 스택

| 영역 | 선택 | 이유 |
|---|---|---|
| 언어 | Python 3.11 | AI 생태계 최적 |
| 의존성 관리 | uv (Astral) | pip보다 빠르고 lockfile 명확 |
| 웹 프레임워크 | FastAPI + uvicorn | async 지원, OpenAPI 자동 생성 |
| 임베딩 | `text-embedding-3-small` (OpenAI) 기본, `BGE-small-en-v1.5` 로컬 fallback | 512-D/384-D 조절 가능 |
| 메인 에이전트 데모 | Claude Sonnet 4.6 (anthropic SDK) | Claude Code 사용자에게 친숙 |
| sLLM Judge | Claude Haiku 4.5 (API) + `llama-cpp-python` + Phi-4-mini (선택) | 빠른 iteration, 로컬 옵션도 구현 |
| 서명 | `cryptography` (Ed25519) | 표준 라이브러리 |
| 저장소 | SQLite (메타데이터) + JSONL append-only (원본 ATV) | MVP 단순성 |
| 정책 엔진 | 자체 JSON rule engine (v1) → OPA (v2) | MVP는 외부 의존성 최소화 |
| 테스트 | pytest + httpx + pytest-asyncio | 표준 |
| 컨테이너 | Docker + docker compose | 로컬 배포 재현성 |
| 타입 체크 | mypy (strict) | 런타임 오류 방지 |
| 린터/포매터 | ruff | 빠름, 통합형 |

---

## 4. 레포 구조

```
aegisdata-mvp/
├── PLAN.md                          # 이 문서
├── CLAUDE.md                        # Claude Code 가이드
├── README.md                        # 사용법 (Claude Code가 생성)
├── pyproject.toml                   # uv 프로젝트
├── uv.lock
├── .env.example                     # API 키 샘플
├── docker-compose.yml
├── Dockerfile
│
├── src/
│   └── aegis/
│       ├── __init__.py
│       ├── schema.py                # ATV 스키마 정의 (pydantic)
│       ├── atv/
│       │   ├── __init__.py
│       │   ├── builder.py           # ATV 생성
│       │   └── embeddings.py        # 임베딩 추상화
│       ├── firewall/
│       │   ├── __init__.py
│       │   ├── core.py              # 메인 오케스트레이터
│       │   ├── step310_args.py
│       │   ├── step320_blast.py
│       │   ├── step330_human.py
│       │   ├── step335_cost.py
│       │   └── step340_policy.py
│       ├── judge/
│       │   ├── __init__.py
│       │   ├── base.py              # Judge 추상 인터페이스
│       │   ├── haiku.py             # Claude Haiku 구현
│       │   └── local_phi.py         # Phi-4-mini 로컬 (옵션)
│       ├── sign/
│       │   ├── __init__.py
│       │   ├── ed25519.py           # 서명/검증
│       │   └── merkle.py            # Merkle 체인
│       ├── audit/
│       │   ├── __init__.py
│       │   ├── sqlite_store.py
│       │   └── jsonl_store.py
│       ├── config.py                # pydantic-settings
│       ├── main.py                  # FastAPI app
│       └── api/
│           ├── __init__.py
│           ├── evaluate.py
│           ├── approve.py
│           └── audit_query.py
│
├── policies/
│   ├── default.json                 # 기본 정책 룰 세트
│   └── tenant_acme.json             # 테넌트별 오버라이드
│
├── demo/
│   ├── agent_demo.py                # Claude Sonnet 4.6 샘플 에이전트
│   ├── tools.py                     # 샘플 툴 정의
│   └── run_scenario.sh
│
└── tests/
    ├── conftest.py
    ├── unit/
    │   ├── test_atv_builder.py
    │   ├── test_step310.py
    │   ├── test_step320.py
    │   ├── test_step335.py
    │   ├── test_step340.py
    │   ├── test_merkle.py
    │   └── test_sign.py
    ├── integration/
    │   ├── test_firewall_e2e.py
    │   └── test_audit_chain.py
    └── fixtures/
        └── sample_atvs.json
```

---

## 5. 환경 설정

### 5.1 필수 요구사항

- Python 3.11+
- uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Docker Desktop (컨테이너 테스트용)
- API 키: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`

### 5.2 초기화 명령

```bash
# Claude Code가 첫 작업으로 실행할 명령들
uv init aegisdata-mvp --python 3.11
cd aegisdata-mvp
uv add fastapi uvicorn[standard] pydantic pydantic-settings \
       anthropic openai cryptography numpy httpx \
       python-multipart orjson structlog
uv add --dev pytest pytest-asyncio pytest-cov httpx ruff mypy \
              types-requests
```

### 5.3 `.env.example`

```env
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# ATV
AEGIS_ATV_VERSION=ATV-2080-v1
AEGIS_TENANT_DEFAULT=demo-tenant

# Embedding
AEGIS_EMBEDDING_PROVIDER=openai     # openai | bge-local
AEGIS_EMBEDDING_MODEL=text-embedding-3-small

# Judge
AEGIS_JUDGE_PROVIDER=haiku          # haiku | local-phi
AEGIS_JUDGE_TEMPERATURE=0.0
AEGIS_JUDGE_SEED=42

# Signing
AEGIS_SIGNING_KEY_PATH=./keys/ed25519.pem
AEGIS_PUBLIC_KEY_PATH=./keys/ed25519.pub

# Audit
AEGIS_AUDIT_DB=./data/audit.sqlite
AEGIS_AUDIT_JSONL=./data/audit.jsonl

# Policy
AEGIS_POLICY_DIR=./policies
```

---

## 6. 모듈별 구현 가이드

각 하위 섹션은 파일 하나 또는 소수의 파일로 끝나는 단위 작업입니다. Claude Code에게 한 섹션씩 넘기기 좋게 구성했습니다.

### 6.1 `src/aegis/schema.py` — ATV 스키마

**핵심 요구사항**: 2,080차원 벡터의 각 인덱스 범위를 Pydantic 모델로 명시. 버전별로 교체 가능해야 함.

```python
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Literal
import numpy as np

ATV_VERSION = "ATV-2080-v1"
ATV_DIM = 2080

# 인덱스 슬라이스 상수 — 특허 스펙 기준
SLICE_HEADER          = slice(0,    64)
SLICE_AGENT_STATE     = slice(64,   576)   # 512-D
SLICE_PLAN            = slice(576,  1088)  # 512-D
SLICE_TOOL_CALL       = slice(1088, 1472)  # 384-D
SLICE_SAFETY_FLAGS    = slice(1472, 1728)  # 256-D
SLICE_MEMORY_FP       = slice(1728, 1864)  # 136-D
SLICE_COST_EFFICIENCY = slice(1864, 1880)  # 16-D
# Hardware band (T2에선 zeros)
SLICE_IO_PROFILE      = slice(1880, 1960)
SLICE_DMA_FANOUT      = slice(1960, 2040)
SLICE_HW_COST         = slice(2040, 2060)
SLICE_LINKAGE         = slice(2060, 2080)
SLICE_DIVERGENCE      = slice(2057, 2060)  # 마지막 3-D

class ATVHeader(BaseModel):
    trace_id: str
    span_id: str
    tenant_id: str
    aid: str
    ats: str = ATV_VERSION
    timestamp_ns: int
    model_hash: str | None = None
    burn_in_id: str | None = None

class CostEfficiency(BaseModel):
    exp_bytes_read: float = 0
    exp_bytes_write: float = 0
    exp_iops: float = 0
    exp_time_ms: float = 0
    exp_net_in: float = 0
    exp_net_out: float = 0
    exp_tokens: float = 0
    exp_api_calls: float = 0
    exp_dollars: float = 0
    confidence: float = 1.0
    flag_high_risk: float = 0
    flag_batch: float = 0
    # 16-D total, 4 reserved
    reserved: list[float] = Field(default_factory=lambda: [0.0] * 4)

    def to_array(self) -> np.ndarray:
        arr = np.array([
            self.exp_bytes_read, self.exp_bytes_write, self.exp_iops,
            self.exp_time_ms, self.exp_net_in, self.exp_net_out,
            self.exp_tokens, self.exp_api_calls, self.exp_dollars,
            self.confidence, self.flag_high_risk, self.flag_batch,
            *self.reserved
        ], dtype=np.float32)
        assert arr.size == 16
        return arr

class ATVInput(BaseModel):
    """에이전트가 /evaluate 에 보내는 입력."""
    header: ATVHeader
    agent_state_text: str
    plan_text: str
    tool_name: str
    tool_args_json: str
    safety_flags: dict[str, float] = Field(default_factory=dict)
    memory_fingerprint: str | None = None
    cost_estimate: CostEfficiency = Field(default_factory=CostEfficiency)

class Verdict(BaseModel):
    decision: Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"]
    reason: str
    atv_id: str
    signature: str | None = None
    confidence: float = 1.0
    step_traces: dict[str, str] = Field(default_factory=dict)
```

### 6.2 `src/aegis/atv/embeddings.py` — 임베딩 추상화

```python
from abc import ABC, abstractmethod
import numpy as np
from openai import OpenAI
from aegis.config import settings

class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, text: str, dim: int) -> np.ndarray: ...

class OpenAIEmbedding(EmbeddingProvider):
    def __init__(self):
        self.client = OpenAI()
        self.model = settings.embedding_model  # text-embedding-3-small

    def embed(self, text: str, dim: int) -> np.ndarray:
        if not text:
            return np.zeros(dim, dtype=np.float32)
        resp = self.client.embeddings.create(
            model=self.model,
            input=text[:8000],  # 토큰 한도 방어
            dimensions=dim,
        )
        return np.array(resp.data[0].embedding, dtype=np.float32)

class BGELocalEmbedding(EmbeddingProvider):
    """sentence-transformers BGE-small-en-v1.5 + truncation/padding으로 dim 맞춤."""
    # Claude Code 주석: MVP 첫 버전은 OpenAI만, 이건 Stretch goal.
    ...

def get_provider() -> EmbeddingProvider:
    match settings.embedding_provider:
        case "openai":
            return OpenAIEmbedding()
        case "bge-local":
            return BGELocalEmbedding()
        case _:
            raise ValueError(f"Unknown provider: {settings.embedding_provider}")
```

### 6.3 `src/aegis/atv/builder.py` — ATV 조립

```python
import numpy as np
import hashlib
from aegis.schema import ATVInput, ATV_DIM, *  # slice constants
from aegis.atv.embeddings import get_provider

def encode_header(h: ATVHeader) -> np.ndarray:
    """64-D: 각 필드를 SHA3 해시 → float32 배열로 flatten. 결정적이어야 함."""
    blob = f"{h.trace_id}|{h.span_id}|{h.tenant_id}|{h.aid}|{h.ats}|{h.timestamp_ns}"
    digest = hashlib.sha3_256(blob.encode()).digest()  # 32 bytes
    # 32 bytes → 64 float32 by splitting each byte as two 4-bit values normalized to [-1, 1]
    arr = np.zeros(64, dtype=np.float32)
    for i, b in enumerate(digest):
        arr[2*i]   = ((b >> 4) - 7.5) / 7.5
        arr[2*i+1] = ((b & 0x0F) - 7.5) / 7.5
    return arr

def encode_safety_flags(flags: dict[str, float]) -> np.ndarray:
    """256-D: 정해진 키 세트를 고정 순서로. 모르는 키는 무시, 없는 키는 0."""
    keys = [
        "prompt_injection", "pii_exposure", "jailbreak",
        "toxicity", "sql_injection", "path_traversal",
        "data_exfiltration", "privilege_escalation",
        # ... 32개 주요 키 + 나머지는 0으로 패딩 → 256-D
    ]
    arr = np.zeros(256, dtype=np.float32)
    for i, k in enumerate(keys[:256]):
        arr[i] = flags.get(k, 0.0)
    return arr

def encode_memory_fp(fp: str | None) -> np.ndarray:
    """136-D: 해시를 float로 전개."""
    if not fp:
        return np.zeros(136, dtype=np.float32)
    digest = hashlib.sha3_512(fp.encode()).digest()  # 64 bytes
    arr = np.zeros(136, dtype=np.float32)
    for i, b in enumerate(digest[:68]):
        arr[2*i]   = ((b >> 4) - 7.5) / 7.5
        arr[2*i+1] = ((b & 0x0F) - 7.5) / 7.5
    return arr

def build_atv(inp: ATVInput) -> np.ndarray:
    emb = get_provider()
    atv = np.zeros(ATV_DIM, dtype=np.float32)
    atv[SLICE_HEADER]          = encode_header(inp.header)
    atv[SLICE_AGENT_STATE]     = emb.embed(inp.agent_state_text, 512)
    atv[SLICE_PLAN]            = emb.embed(inp.plan_text, 512)
    atv[SLICE_TOOL_CALL]       = emb.embed(f"{inp.tool_name}({inp.tool_args_json})", 384)
    atv[SLICE_SAFETY_FLAGS]    = encode_safety_flags(inp.safety_flags)
    atv[SLICE_MEMORY_FP]       = encode_memory_fp(inp.memory_fingerprint)
    atv[SLICE_COST_EFFICIENCY] = inp.cost_estimate.to_array()
    # Hardware band remains zeros for T2
    return atv
```

### 6.4 Action Firewall 단계별 구현

각 step 파일은 `(atv: np.ndarray, inp: ATVInput) -> StepResult` 시그니처를 가집니다.

```python
# src/aegis/firewall/core.py
from dataclasses import dataclass
import numpy as np

@dataclass
class StepResult:
    verdict: str | None     # None = 다음 단계로, "BLOCK"/"REQUIRE_APPROVAL" = 즉시 확정, "ALLOW" = 그대로 통과
    reason: str
    trace: str

def run_firewall(atv, inp) -> Verdict:
    from aegis.firewall import (
        step310_args, step320_blast, step330_human,
        step335_cost, step340_policy,
    )
    traces = {}
    for fn in [step310_args.run, step320_blast.run, step330_human.run,
               step335_cost.run, step340_policy.run]:
        r = fn(atv, inp)
        traces[fn.__module__] = r.trace
        if r.verdict in ("BLOCK", "REQUIRE_APPROVAL"):
            return Verdict(decision=r.verdict, reason=r.reason,
                           atv_id=..., step_traces=traces)
    return Verdict(decision="ALLOW", reason="all passed", ..., step_traces=traces)
```

#### Step 310 — Argument Inspection

```python
# src/aegis/firewall/step310_args.py
import re
from aegis.schema import SLICE_SAFETY_FLAGS
from aegis.firewall.core import StepResult

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\s+/",
    r"DROP\s+TABLE",
    r"/etc/(shadow|passwd)",
    r"\bsudo\s+",
    r"\b(exec|system)\s*\(",
]

INJECTION_THRESHOLD = 0.7

def run(atv, inp) -> StepResult:
    # 1) 정적 패턴 검사
    payload = inp.tool_args_json.lower()
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, payload, re.IGNORECASE):
            return StepResult("BLOCK", f"dangerous pattern: {pat}",
                              "step310: static pattern hit")

    # 2) safety_flags 에서 인젝션 점수 확인
    inj = inp.safety_flags.get("prompt_injection", 0.0)
    if inj > INJECTION_THRESHOLD:
        return StepResult("BLOCK", f"prompt injection score {inj:.2f}",
                          "step310: safety score breach")

    return StepResult(None, "", f"step310: ok (inj={inj:.2f})")
```

#### Step 320 — Blast Radius

```python
# src/aegis/firewall/step320_blast.py
TOOL_BLAST_TABLE = {
    "read_file": 1,
    "write_file": 3,
    "execute_shell": 8,
    "call_external_api": 5,
    "send_email": 6,
    "db_query": 2,
    "db_mutation": 7,
    "transfer_funds": 10,
}

def run(atv, inp) -> StepResult:
    blast = TOOL_BLAST_TABLE.get(inp.tool_name, 5)  # unknown tools default to medium
    # 블래스트를 context에 저장 — 다음 단계들이 참조
    inp.model_config.update({"blast_radius": blast})
    return StepResult(None, "", f"step320: blast={blast}")
```

#### Step 330 — Human Oversight

```python
# src/aegis/firewall/step330_human.py
HIGH_BLAST_THRESHOLD = 7

def run(atv, inp) -> StepResult:
    blast = getattr(inp, "_blast_radius", 5)
    if blast >= HIGH_BLAST_THRESHOLD:
        return StepResult(
            "REQUIRE_APPROVAL",
            f"blast radius {blast} >= {HIGH_BLAST_THRESHOLD}",
            "step330: human approval required",
        )
    return StepResult(None, "", f"step330: ok (blast={blast})")
```

#### Step 335 — Forecasted Cost

```python
# src/aegis/firewall/step335_cost.py
from aegis.config import settings

# 간단한 테넌트 예산 (실전은 DB 조회)
TENANT_BUDGETS = {
    "demo-tenant": {"bytes": 1e9, "dollars": 1.0, "time_ms": 60000},
}

def run(atv, inp) -> StepResult:
    budget = TENANT_BUDGETS.get(inp.header.tenant_id, TENANT_BUDGETS["demo-tenant"])
    ce = inp.cost_estimate

    if ce.exp_bytes_write > budget["bytes"]:
        return StepResult("REQUIRE_APPROVAL",
                          f"exp_bytes_write {ce.exp_bytes_write} > budget {budget['bytes']}",
                          "step335: byte budget exceeded")
    if ce.exp_dollars > budget["dollars"]:
        return StepResult("REQUIRE_APPROVAL",
                          f"exp_dollars {ce.exp_dollars} > budget {budget['dollars']}",
                          "step335: dollar budget exceeded")
    if ce.confidence < 0.3:
        return StepResult("REQUIRE_APPROVAL",
                          f"cost confidence too low: {ce.confidence}",
                          "step335: low confidence")

    return StepResult(None, "", f"step335: ok")
```

#### Step 340 — Policy + sLLM fallback

```python
# src/aegis/firewall/step340_policy.py
import json
from pathlib import Path
from aegis.config import settings
from aegis.firewall.core import StepResult
from aegis.judge import get_judge

def load_policies():
    path = Path(settings.policy_dir) / "default.json"
    return json.loads(path.read_text())

def run(atv, inp) -> StepResult:
    rules = load_policies()
    # 1) 명시적 deny 룰
    for rule in rules.get("deny", []):
        if match_rule(rule, inp):
            return StepResult("BLOCK", f"policy deny: {rule['name']}",
                              f"step340: deny match {rule['name']}")
    # 2) 명시적 allow 룰
    for rule in rules.get("allow", []):
        if match_rule(rule, inp):
            return StepResult(None, "", f"step340: allow match {rule['name']}")
    # 3) sLLM fallback
    judge = get_judge()
    verdict = judge.evaluate(atv_summary_for_llm(inp))
    if verdict.decision == "ALLOW":
        return StepResult(None, "", f"step340: sLLM allow conf={verdict.confidence}")
    elif verdict.decision == "BLOCK":
        return StepResult("BLOCK", verdict.reason, f"step340: sLLM block")
    else:
        return StepResult("REQUIRE_APPROVAL", verdict.reason, f"step340: sLLM approval")

def match_rule(rule, inp) -> bool:
    # 간단한 predicate: tool_name, tenant_id, arg 정규식
    if "tool_name" in rule and rule["tool_name"] != inp.tool_name:
        return False
    if "tenant_id" in rule and rule["tenant_id"] != inp.header.tenant_id:
        return False
    if "arg_pattern" in rule:
        import re
        if not re.search(rule["arg_pattern"], inp.tool_args_json):
            return False
    return True

def atv_summary_for_llm(inp) -> str:
    return f"""Tool: {inp.tool_name}
Args: {inp.tool_args_json[:500]}
Tenant: {inp.header.tenant_id}
Plan: {inp.plan_text[:300]}
Safety scores: {inp.safety_flags}
Cost estimate: bytes={inp.cost_estimate.exp_bytes_write}, $={inp.cost_estimate.exp_dollars}"""
```

### 6.5 `src/aegis/judge/haiku.py` — sLLM via Claude Haiku

```python
from anthropic import Anthropic
from aegis.judge.base import Judge, JudgeVerdict
import json, os

SYSTEM_PROMPT = """You are a deterministic 3-class security classifier for AI agent tool calls.
Respond with ONLY a JSON object, no prose:
{"decision":"ALLOW|BLOCK|REQUIRE_APPROVAL","confidence":0.0-1.0,"reason":"..."}

Decide BLOCK if: clear malicious intent, data exfiltration, privilege escalation,
  destructive command against critical paths, clear policy violation.
Decide REQUIRE_APPROVAL if: ambiguous intent, unusual pattern, high-impact operation
  without sufficient context, or cost/scope beyond typical.
Decide ALLOW if: routine safe operation, well-within-scope, no red flags."""

class HaikuJudge(Judge):
    def __init__(self):
        self.client = Anthropic()
        self.model = "claude-haiku-4-5-20251001"

    def evaluate(self, summary: str) -> JudgeVerdict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=200,
            temperature=0.0,  # 결정성
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": summary}],
        )
        text = resp.content[0].text.strip()
        # JSON 추출 (약한 파싱 방어)
        start = text.find("{")
        end = text.rfind("}") + 1
        data = json.loads(text[start:end])
        return JudgeVerdict(
            decision=data["decision"],
            confidence=float(data.get("confidence", 0.5)),
            reason=data.get("reason", ""),
        )
```

### 6.6 `src/aegis/sign/ed25519.py` — 서명

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey
)
from cryptography.hazmat.primitives import serialization
from pathlib import Path
import hashlib, json, time

def load_or_create_key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    # 공개키도 저장
    pub_path = path.with_suffix(".pub")
    pub_path.write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return key

def sign_atv(atv_bytes: bytes, header: dict, prev_hash: str,
             key: Ed25519PrivateKey) -> dict:
    payload = {
        "atv_sha3_256": hashlib.sha3_256(atv_bytes).hexdigest(),
        "header": header,
        "prev_hash": prev_hash,
        "signed_at_ns": time.time_ns(),
    }
    msg = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = key.sign(msg)
    return {
        "payload": payload,
        "signature": sig.hex(),
        "algorithm": "Ed25519",
    }

def verify(record: dict, pub_key: Ed25519PublicKey) -> bool:
    msg = json.dumps(record["payload"], sort_keys=True, separators=(",", ":")).encode()
    try:
        pub_key.verify(bytes.fromhex(record["signature"]), msg)
        return True
    except Exception:
        return False
```

### 6.7 `src/aegis/audit/sqlite_store.py` + `jsonl_store.py`

```python
# sqlite_store.py: 인덱스와 메타데이터
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  atv_id      TEXT PRIMARY KEY,
  aid         TEXT NOT NULL,
  tenant_id   TEXT NOT NULL,
  tool_name   TEXT NOT NULL,
  decision    TEXT NOT NULL,
  timestamp_ns INTEGER NOT NULL,
  prev_hash   TEXT,
  this_hash   TEXT NOT NULL,
  signature   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aid_ts ON audit(aid, timestamp_ns);
CREATE TABLE IF NOT EXISTS chain_head (
  aid TEXT PRIMARY KEY,
  last_hash TEXT NOT NULL
);
"""

class AuditDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, isolation_level="IMMEDIATE")
        self.conn.executescript(SCHEMA)

    def append(self, record: dict):
        # transaction: 체인 head 조건부 업데이트
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            aid = record["payload"]["header"]["aid"]
            prev = record["payload"]["prev_hash"]
            row = cur.execute("SELECT last_hash FROM chain_head WHERE aid=?",
                              (aid,)).fetchone()
            current = row[0] if row else "GENESIS"
            if current != prev:
                raise RuntimeError(f"chain break: expected {current}, got {prev}")
            cur.execute("""INSERT INTO audit
                (atv_id, aid, tenant_id, tool_name, decision, timestamp_ns,
                 prev_hash, this_hash, signature) VALUES (?,?,?,?,?,?,?,?,?)""",
                (record["atv_id"], aid, record["payload"]["header"]["tenant_id"],
                 record["payload"]["tool_name"], record["decision"],
                 record["payload"]["signed_at_ns"], prev, record["this_hash"],
                 record["signature"]))
            cur.execute("""INSERT INTO chain_head(aid, last_hash) VALUES (?,?)
                ON CONFLICT(aid) DO UPDATE SET last_hash=excluded.last_hash""",
                (aid, record["this_hash"]))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_chain(self, aid: str):
        return self.conn.execute(
            "SELECT * FROM audit WHERE aid=? ORDER BY timestamp_ns", (aid,)
        ).fetchall()

    def get_head(self, aid: str) -> str:
        row = self.conn.execute(
            "SELECT last_hash FROM chain_head WHERE aid=?", (aid,)).fetchone()
        return row[0] if row else "GENESIS"
```

```python
# jsonl_store.py: append-only 원본 ATV 덤프
from pathlib import Path
import json, gzip

class JsonlStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict):
        with self.path.open("a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
            f.flush()

    def read_all(self):
        with self.path.open() as f:
            yield from (json.loads(l) for l in f if l.strip())
```

### 6.8 FastAPI 진입점

```python
# src/aegis/main.py
from fastapi import FastAPI, HTTPException
from aegis.schema import ATVInput, Verdict
from aegis.atv.builder import build_atv
from aegis.firewall.core import run_firewall
from aegis.sign.ed25519 import load_or_create_key, sign_atv
from aegis.audit.sqlite_store import AuditDB
from aegis.audit.jsonl_store import JsonlStore
from aegis.config import settings
import hashlib, uuid

app = FastAPI(title="AegisData T2", version="0.1.0")
key = load_or_create_key(Path(settings.signing_key_path))
db  = AuditDB(settings.audit_db)
log = JsonlStore(Path(settings.audit_jsonl))

@app.post("/evaluate", response_model=Verdict)
def evaluate(inp: ATVInput) -> Verdict:
    atv = build_atv(inp)
    verdict = run_firewall(atv, inp)
    atv_id = str(uuid.uuid4())
    verdict.atv_id = atv_id

    # 서명 및 감사 로깅
    prev = db.get_head(inp.header.aid)
    header_dict = inp.header.model_dump() | {"decision": verdict.decision,
                                              "tool_name": inp.tool_name}
    record = sign_atv(atv.tobytes(), header_dict, prev, key)
    record["atv_id"] = atv_id
    record["decision"] = verdict.decision
    record["payload"]["tool_name"] = inp.tool_name
    record["this_hash"] = hashlib.sha3_256(
        json.dumps(record["payload"], sort_keys=True).encode()).hexdigest()

    log.append(record)
    db.append(record)
    verdict.signature = record["signature"]
    return verdict

@app.get("/audit/{aid}")
def audit_chain(aid: str):
    return {"chain": db.get_chain(aid), "head": db.get_head(aid)}

@app.get("/healthz")
def health():
    return {"ok": True}
```

### 6.9 `policies/default.json` 샘플

```json
{
  "deny": [
    {"name": "no-etc-shadow", "arg_pattern": "/etc/shadow"},
    {"name": "no-drop-table", "arg_pattern": "DROP\\s+TABLE", "tool_name": "db_query"}
  ],
  "allow": [
    {"name": "safe-read", "tool_name": "read_file", "arg_pattern": "^\\./data/"},
    {"name": "safe-list", "tool_name": "list_directory"}
  ]
}
```

### 6.10 데모 에이전트 (`demo/agent_demo.py`)

```python
from anthropic import Anthropic
import httpx, json, time, uuid

AEGIS_URL = "http://localhost:8000"

TOOLS = [
    {"name": "read_file", "description": "Read a file",
     "input_schema": {"type": "object", "properties":
                      {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write a file",
     "input_schema": {"type": "object", "properties":
                      {"path": {"type":"string"},"content":{"type":"string"}},
                      "required":["path","content"]}},
    {"name": "execute_shell", "description": "Run a shell command",
     "input_schema": {"type":"object","properties":
                      {"command":{"type":"string"}},"required":["command"]}},
    {"name": "db_query", "description": "Run SQL query",
     "input_schema": {"type":"object","properties":
                      {"sql":{"type":"string"}},"required":["sql"]}},
    {"name": "transfer_funds", "description": "Transfer money",
     "input_schema": {"type":"object","properties":
                      {"from":{"type":"string"},"to":{"type":"string"},
                       "amount":{"type":"number"}},
                      "required":["from","to","amount"]}},
]

def ask_aegis(tool_name, tool_args, plan_text, trace_id, aid="agent-demo"):
    payload = {
        "header": {"trace_id": trace_id, "span_id": str(uuid.uuid4()),
                   "tenant_id": "demo-tenant", "aid": aid,
                   "ats": "ATV-2080-v1", "timestamp_ns": time.time_ns()},
        "agent_state_text": "demo agent running scenario",
        "plan_text": plan_text,
        "tool_name": tool_name,
        "tool_args_json": json.dumps(tool_args),
        "safety_flags": {},
        "cost_estimate": {"exp_bytes_write": len(json.dumps(tool_args)) * 100,
                          "exp_dollars": 0.001, "confidence": 0.8},
    }
    r = httpx.post(f"{AEGIS_URL}/evaluate", json=payload, timeout=30.0)
    r.raise_for_status()
    return r.json()

def main():
    client = Anthropic()
    trace = str(uuid.uuid4())
    user_msg = "Please: 1) read ./data/report.txt 2) write a summary to " \
               "./data/summary.txt 3) run `ls` 4) query the DB for user count " \
               "5) transfer $500 from acct A to B."
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=TOOLS,
        messages=[{"role": "user", "content": user_msg}],
    )
    # tool_use 블록 순회, 각 호출 전 aegis에 질의
    for block in msg.content:
        if block.type == "tool_use":
            verdict = ask_aegis(block.name, block.input,
                                plan_text=user_msg, trace_id=trace)
            print(f"[{block.name}] → {verdict['decision']} ({verdict['reason']})")

if __name__ == "__main__":
    main()
```

---

## 7. 테스트 전략

### 7.1 단위 테스트 (pytest)

- `test_atv_builder.py` — 2,080 차원 정확성, 인덱스 슬라이스 매핑 검증
- `test_step310.py` — 알려진 위험 패턴 5개 전부 BLOCK, 정상 인자는 통과
- `test_step320.py` — 알려진 툴 10개의 블래스트 값 확인
- `test_step335.py` — 예산 초과 시 REQUIRE_APPROVAL
- `test_step340.py` — 정책 파일에 따른 allow/deny 매칭 + sLLM 호출 mock
- `test_merkle.py` — 체인 링크 검증, 중간 삽입/변조 탐지
- `test_sign.py` — Ed25519 round-trip, 잘못된 서명 탐지

### 7.2 통합 테스트

- `test_firewall_e2e.py` — FastAPI 서버를 TestClient로 띄우고 10개 시나리오 호출
- `test_audit_chain.py` — 100개 요청 후 체인 무결성 검증 + 체인 위조 탐지

### 7.3 성능 테스트 (선택)

- 단순 tool call 1,000 req → p95 < 500ms 목표 (임베딩은 캐시, sLLM은 경계 사례만)

---

## 8. 개발 타임라인 (Day 1-7)

하루 평균 3-5시간 투입 기준.

| Day | 목표 | 산출물 |
|---|---|---|
| 1 | 레포·CI·환경 세팅, `schema.py`, 설정 로딩 | uv 프로젝트, 타입 통과, .env 로드 |
| 2 | `atv/` 모듈 완성 + 단위 테스트 | `build_atv()` 완성, OpenAI 임베딩 연결 |
| 3 | Firewall Step 310-335 구현 + 테스트 | 4개 step 녹색 |
| 4 | Step 340 (정책 + Haiku sLLM) | sLLM 연결, 정책 파일 로딩, e2e BLOCK/ALLOW 확인 |
| 5 | 서명 + 감사 로그 | SQLite + JSONL, 체인 head 조건부 업데이트 |
| 6 | FastAPI 통합, Docker Compose | `docker compose up` 녹색, `/evaluate` 동작 |
| 7 | 데모 에이전트 + 문서 + README 보강 | 5개 시나리오 출력 캡처, 커버리지 ≥70% |

여유가 있으면 Day 8-9에:
- 로컬 Phi-4-mini judge
- Prometheus 메트릭 endpoint
- Attestation stub (Burn-in L3~L5 메타데이터)

---

## 9. Claude Code 사용 가이드

### 9.1 프로젝트 루트에 둘 `CLAUDE.md` 템플릿

```markdown
# Aegis MVP - Claude Code Project Guide

## Project Context
이 프로젝트는 AegisData 특허 v4의 T2 티어 MVP 구현입니다.
핵심 설계는 `PLAN.md`에 있습니다. 변경 전에 반드시 해당 섹션을 읽으세요.

## Architecture
- FastAPI 서비스 (`src/aegis/main.py`)
- 7-step Action Firewall (`src/aegis/firewall/`)
- Claude Haiku 기반 sLLM judge
- Ed25519 + Merkle-chained SQLite 감사 로그

## Commands
- `uv sync` — 의존성 설치
- `uv run pytest` — 테스트
- `uv run ruff check . && uv run mypy src` — 린트·타입
- `uv run uvicorn aegis.main:app --reload` — 개발 서버
- `docker compose up --build` — 전체 스택

## Code Style
- Python 3.11, type hints 필수
- Pydantic v2 모델로 데이터 경계 표현
- async 함수는 FastAPI handler에서만 (내부 로직은 sync)
- 모든 외부 호출(OpenAI, Anthropic)은 retry + timeout 필수
- 로깅은 structlog 사용, `structlog.get_logger()`

## Testing Rules
- 새 step 함수 추가 시 반드시 `tests/unit/test_stepXXX.py` 동반
- 통합 테스트는 Anthropic API를 mock 처리 (pytest fixture)
- 커버리지 70% 유지

## Security Notes
- Ed25519 private key는 `./keys/`에만 존재, 커밋 금지 (.gitignore)
- API 키는 `.env` 에서 로드, 절대 하드코딩 금지
- 감사 로그는 append-only — 기존 레코드 수정/삭제 코드 작성 금지

## Where Things Live
- ATV 스키마: `src/aegis/schema.py`
- Firewall: `src/aegis/firewall/step*.py`
- Policies: `policies/*.json`
- Demo: `demo/agent_demo.py`
```

### 9.2 Claude Code 단계별 프롬프트 예시

Claude Code CLI에서 쓸 자연스러운 명령 시퀀스:

**Milestone 1 — 프로젝트 부트스트랩**
```
claude "PLAN.md를 읽고 Section 4의 레포 구조대로 디렉터리와 빈 __init__.py들을
생성해줘. Section 5의 uv 명령으로 프로젝트를 초기화하고 pyproject.toml, .env.example,
docker-compose.yml 스켈레톤까지 만들어줘. 완료 후 `uv sync`가 성공하는지 확인해줘."
```

**Milestone 2 — 스키마와 ATV 빌더**
```
claude "PLAN.md Section 6.1-6.3을 구현해줘: schema.py, atv/embeddings.py,
atv/builder.py. 각 파일에 docstring과 타입 주석을 달고, tests/unit/test_atv_builder.py도
같이 만들어. OpenAI API 키가 없으면 에러 없이 dummy 임베딩으로 동작하게 fallback도 추가해."
```

**Milestone 3 — Firewall Steps**
```
claude "PLAN.md Section 6.4의 Step 310-335를 구현해줘 (340은 다음 단계). 각 step 파일은
5-40줄 정도로 짧게 유지하고, 공통 타입은 firewall/core.py의 StepResult를 쓴다.
tests/unit/test_stepXXX.py도 각각 만들어서, 대표 BLOCK/ALLOW/APPROVAL 시나리오를
최소 3개씩 커버해."
```

**Milestone 4 — Policy + Haiku Judge**
```
claude "PLAN.md Section 6.4(Step 340) + Section 6.5를 구현해줘. judge 추상 인터페이스
(judge/base.py) 먼저 만들고 HaikuJudge 구현체를 넣어. step340_policy.py는 default.json을
로드해서 deny/allow 룰로 먼저 판정하고, 어느 쪽에도 안 걸리면 Judge를 호출해.
테스트에서는 respx로 Anthropic API를 mock해서 결정적으로 테스트해."
```

**Milestone 5 — 서명 + 감사**
```
claude "PLAN.md Section 6.6 + 6.7을 구현해줘. Ed25519 키가 없으면 자동 생성하고 ./keys/에
저장하게 해. Merkle 체인 링크 로직은 sqlite transaction 안에서 체인 head를 조건부로
업데이트해야 해 — 동시성 테스트(pytest-asyncio로 100개 동시 요청)도 추가해."
```

**Milestone 6 — FastAPI + 통합**
```
claude "PLAN.md Section 6.8을 구현해줘. /evaluate, /audit/{aid}, /healthz. 
TestClient로 테스트 짜서 e2e 플로우를 검증해 — BLOCK 시나리오, ALLOW 시나리오,
REQUIRE_APPROVAL 시나리오, 체인 무결성 시나리오 네 가지. docker-compose.yml도
실제로 빌드·구동되는지 확인해."
```

**Milestone 7 — 데모**
```
claude "PLAN.md Section 6.10 데모를 실행 가능하게 완성해줘. 실제 Anthropic API 키가
있다는 전제 하에 5개 시나리오를 연달아 실행하고 결과를 colorized stdout으로 찍어줘.
demo/run_scenario.sh는 docker-compose 기동부터 demo 실행까지 한 번에 하는 편의 스크립트."
```

### 9.3 자연스러운 루프

Claude Code는 "계획 → 구현 → 테스트 → 커밋" 루프를 선호합니다. 각 Milestone 끝에서:

```
claude "방금 만든 모듈에 대해 pytest 돌리고, ruff/mypy도 통과하는지 확인한 뒤
의미 있는 단위로 git commit 해줘. 커밋 메시지는 conventional commits로."
```

### 9.4 디버깅 시 유용한 프롬프트

```
claude "최근 테스트 하나가 깨졌어. `pytest tests/unit/test_step340.py -xvs` 돌려보고,
실패 원인을 찾아서 고쳐줘. 정책 매칭 로직인지 Judge mock인지 먼저 구분해서 보고해."
```

```
claude "방금 추가한 비동기 Merkle 업데이트에 race condition 위험이 있어. 
`src/aegis/audit/sqlite_store.py`의 append() 함수를 검토해서 동시 삽입 시에도
체인 head가 깨지지 않는지 분석하고, 필요하면 transaction isolation을 강화해줘."
```

---

## 10. 확장 로드맵 (T3로 가는 길)

MVP가 안정화된 뒤 다음 순서로 기능 추가:

1. **Burn-in L3 추가**: 프로세스 시작 시 MRENCLAVE-유사한 hash 생성 (TEE 없이 소프트웨어 에뮬레이션)
2. **Attestation stub**: `/attestation` 엔드포인트에서 측정값 조회 (JSON으로)
3. **Nitro Enclave 이식**: `nitro-cli`로 enclave 이미지 빌드, KMS 기반 키 봉인
4. **하드웨어 밴드 추정**: eBPF/iostat로 실제 syscall/IO 측정해서 200-D 중 일부 실데이터 채움
5. **Post-quantum 서명**: `liboqs`로 ML-DSA 이중 서명
6. **CSD 통합**: SmartSSD/ScaleFlux API 테스트 환경 — 별도 프로젝트
7. **Vector DB 감사 검색**: ATV 임베딩들의 벡터 유사도 쿼리로 "유사 공격 패턴" 탐색

---

## 11. 부록

### 11.1 환경 변수 전체 목록

`.env`의 모든 필드는 `src/aegis/config.py`의 `Settings(BaseSettings)` 클래스에서 관리. 누락 시 기본값 또는 에러.

### 11.2 샘플 요청 페이로드

```json
POST /evaluate
{
  "header": {
    "trace_id": "t-001", "span_id": "s-001",
    "tenant_id": "demo-tenant", "aid": "agent-42",
    "ats": "ATV-2080-v1", "timestamp_ns": 1737172800000000000
  },
  "agent_state_text": "User asked for a file summary...",
  "plan_text": "Read the file, then write summary.",
  "tool_name": "read_file",
  "tool_args_json": "{\"path\":\"./data/report.txt\"}",
  "safety_flags": {"prompt_injection": 0.02, "pii_exposure": 0.00},
  "memory_fingerprint": "sha3_256:abcdef01234567...",
  "cost_estimate": {
    "exp_bytes_write": 1024, "exp_dollars": 0.0001,
    "exp_time_ms": 80, "confidence": 0.9
  }
}
```

응답:
```json
{
  "decision": "ALLOW",
  "reason": "all passed",
  "atv_id": "6f8b7c5a-...",
  "signature": "ed25519:7a2c...",
  "confidence": 1.0,
  "step_traces": {
    "aegis.firewall.step310_args": "step310: ok (inj=0.02)",
    "aegis.firewall.step320_blast": "step320: blast=1",
    "aegis.firewall.step330_human": "step330: ok (blast=1)",
    "aegis.firewall.step335_cost": "step335: ok",
    "aegis.firewall.step340_policy": "step340: allow match safe-read"
  }
}
```

### 11.3 Claude Code에게 처음 건네는 프롬프트 (한 방 버전)

프로젝트를 완전히 비어 있는 디렉터리에서 시작하는 경우:

```
claude "PLAN.md 파일이 이 디렉터리에 있습니다. 그 문서를 전부 읽고,
Section 9의 Milestone 1~7 순서대로 구현해주세요.

각 Milestone 끝에서 반드시:
1) pytest를 돌려 녹색 확인
2) ruff check . && mypy src 통과
3) 의미 있는 단위로 git commit (conventional commits)
4) 내게 현재 상태 요약 보고 후 다음 Milestone으로 진행

막히는 부분이 있으면 추측하지 말고 PLAN.md 해당 섹션을 다시 인용해서 내게 질문하세요.
외부 API 키(.env)는 제가 제공하기 전까지는 dummy/mock로 동작하도록 구현하세요."
```

---

**문서 끝**. 이 PLAN.md와 CLAUDE.md 템플릿(Section 9.1)을 프로젝트 루트에 두면, Claude Code가 7일 내 MVP를 완성할 수 있는 자족적인 레퍼런스가 됩니다.
