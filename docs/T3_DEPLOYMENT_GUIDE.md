# T3 Deployment Guide

**대상:** AegisData 를 **실 TEE silicon** (Intel TDX / AMD SEV-SNP / NVIDIA H100 CC) 환경에 배포하려는 운영자.
**전제:** v4.4 코드 (TEE auto-detect + verifier + sealed key abstraction).

---

## 0. 한 줄 요약

> AegisData 는 **TEE 환경에서 코드 변경 없이** 자동 활성화됩니다.
> ``/dev/tdx_guest`` 또는 ``/dev/sev-guest`` 가 존재하면 collector 가
> mock → real 로 swap, 동시에 quote endpoint 가 진짜 attestation
> report 를 발행합니다.

---

## 1. 지원 환경

| Cloud / Hardware | TEE | Aegis 동작 | 비고 |
|---|---|---|---|
| **Azure Confidential VM** (DCsv5/ECsv5) | Intel TDX | ✅ auto-detect | `/dev/tdx_guest` |
| **GCP C3 Confidential** | Intel TDX | ✅ auto-detect | 같음 |
| **AWS EC2 R7iz / M7i Confidential** | Intel TDX | ✅ auto-detect | 같음 |
| **AWS EC2 r6i / m6i with Nitro Enclaves** | AWS Nitro | ⚠️ partial | Nitro attestation 별도 (v4.5 milestone) |
| **AWS r7iz with SEV-SNP** | AMD SEV-SNP | ✅ auto-detect | `/dev/sev-guest` |
| **Azure DCadsv5** | AMD SEV-SNP | ✅ auto-detect | 같음 |
| **GCP n2d Confidential** | AMD SEV-SNP | ✅ auto-detect | 같음 |
| **NVIDIA H100 CC** | GPU TEE + host TDX/SNP | ✅ partial | GPU CC 는 별도 verifier |
| **On-prem Intel SGX** | SGX (legacy) | ❌ | DCAP 만 지원 — TDX 권장 |
| **ARM CCA (Realm)** | ARM CCA | 🟡 stub | v4.5 milestone |

---

## 2. Quick Start — Azure Confidential VM (TDX)

### 2.1 VM 생성

```bash
az vm create \
  --resource-group aegis-rg \
  --name aegis-tdx-1 \
  --image Ubuntu2404Pro \
  --size Standard_DC4as_v5 \
  --security-type ConfidentialVM \
  --enable-secure-boot true \
  --enable-vtpm true \
  --os-disk-security-encryption-type DiskWithVMGuestState
```

### 2.2 OS 안에서 TEE device 확인

```bash
$ ls -la /dev/tdx_guest
crw------- 1 root root 10, 123 Apr 29 12:00 /dev/tdx_guest

$ cat /proc/cpuinfo | grep -m1 tdx_guest
flags : ... tdx_guest ...
```

### 2.3 Aegis sidecar 배포

```bash
# /dev/tdx_guest 를 컨테이너에 mount:
docker run -d \
  --name aegis-sidecar \
  --device /dev/tdx_guest:/dev/tdx_guest \
  -p 8080:8000 \
  -e AEGIS_TEE_PROVIDER=tdx \
  -e AEGIS_HW_PROVIDER=real \
  -e AEGIS_AUDIT_PATROL_ENABLED=true \
  -e AEGIS_IDENTITY_REQUIRE=true \
  -v ./data:/app/data \
  -v ./keys:/app/keys \
  ghcr.io/happyikas/aegis:v4.4.0
```

### 2.4 검증

```bash
# 1. Health check:
curl http://localhost:8080/healthz

# 2. TEE 활성화 확인:
curl http://localhost:8080/attestation/tee | jq '.provider'
# → "tdx"

# 3. 실 quote 가 verifier 를 통과하는지:
curl http://localhost:8080/attestation/tee | jq '.verification.valid'
# → true (schema-only 검증)

# 4. HW band 가 실 데이터로 채워지는지:
curl -X POST http://localhost:8080/evaluate -d '...' | jq '.atv_id'
```

---

## 3. Quick Start — AWS r7iz (SEV-SNP)

### 3.1 EC2 instance launch

```bash
aws ec2 run-instances \
  --image-id ami-XXXXX \
  --instance-type r7iz.metal-16xl \
  --key-name my-key \
  --security-group-ids sg-XXXXX \
  --user-data file://./cloud-init.yaml
```

`cloud-init.yaml`:
```yaml
#cloud-config
packages:
  - linux-image-generic-hwe-22.04
write_files:
  - path: /etc/modules-load.d/sev-guest.conf
    content: sev-guest
runcmd:
  - reboot
```

### 3.2 OS 안에서 device 확인

```bash
$ ls -la /dev/sev-guest
crw------- 1 root root 10, 124 Apr 29 12:00 /dev/sev-guest

$ dmesg | grep SEV-SNP
[    0.123] SEV-SNP: detected, ASID 0
```

### 3.3 컨테이너 배포 (Docker)

```bash
docker run -d \
  --name aegis-sidecar \
  --device /dev/sev-guest:/dev/sev-guest \
  -p 8080:8000 \
  -e AEGIS_TEE_PROVIDER=sev-snp \
  -e AEGIS_HW_PROVIDER=real \
  ghcr.io/happyikas/aegis:v4.4.0
```

---

## 4. NVIDIA H100 Confidential Computing

H100 CC 는 **2-tier attestation**:
1. **Host TEE** (TDX/SEV-SNP) — Aegis 가 처리
2. **GPU CC quote** — 별도 NVIDIA Attestation Service (NAS)

v4.4 는 host attestation 만 자동 활성화. GPU CC quote 는 v4.5 milestone (NVIDIA NAS 통합).

```bash
# H100 CC 환경에서 host attestation 만 활성화:
docker run -d \
  --gpus all \
  --device /dev/tdx_guest:/dev/tdx_guest \
  -e AEGIS_TEE_PROVIDER=tdx \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,confidential \
  ghcr.io/happyikas/aegis:v4.4.0
```

GPU 자체의 launch measurement 검증은 NVIDIA NAS API 별도 호출:
```bash
curl https://nas.attestation.nvidia.com/v1/attest/gpu \
  -H "Authorization: Bearer $NAS_TOKEN" \
  -d @gpu_evidence.json
```

(v4.5 에서 통합 예정)

---

## 5. 환경 변수 reference

### 5.1 TEE 활성화

| Variable | Default | Description |
|---|---|---|
| `AEGIS_TEE_PROVIDER` | `none` (auto-detect) | `none` / `mock` / `tdx` / `sev-snp` |
| `AEGIS_HW_PROVIDER` | `none` | `real` 시 v4.1 collector aggregator 활성화 |
| `AEGIS_TEE_SEAL_KEYS` | `auto` | TEE 봉인 활성화 (`true` / `false` / `auto`) |

### 5.2 보안 강화 (production 권장)

| Variable | Default | Description |
|---|---|---|
| `AEGIS_IDENTITY_REQUIRE` | `false` | `true` 시 모든 호출에 identity proof 강제 |
| `AEGIS_AUDIT_PATROL_ENABLED` | `false` | `true` 시 v4.0 patrol daemon 활성화 |
| `AEGIS_JOURNAL_GROUP_COMMIT` | `false` | `true` 시 v3.8 group commit (high throughput) |
| `AEGIS_TIERED_ARCHIVE_COLD_DIR` | empty | v3.9 cold tier path |

### 5.3 추천 production config (전체)

```bash
# Identity
AEGIS_IDENTITY_REQUIRE=true

# TEE
AEGIS_TEE_PROVIDER=tdx        # 또는 sev-snp
AEGIS_HW_PROVIDER=real
AEGIS_TEE_SEAL_KEYS=auto

# Durability
AEGIS_JOURNAL_GROUP_COMMIT=true
AEGIS_JOURNAL_GROUP_COMMIT_BATCH_SIZE=100
AEGIS_TIERED_ARCHIVE_COLD_DIR=/mnt/cold-s3
AEGIS_PERF_FEEDBACK_SNAPSHOT_DB=/data/perf_feedback.sqlite

# Patrol
AEGIS_AUDIT_PATROL_ENABLED=true
AEGIS_AUDIT_PATROL_FULL_INTERVAL_SEC=21600
AEGIS_AUDIT_PATROL_SAMPLE_INTERVAL_SEC=3600
AEGIS_AUDIT_PATROL_SEQUENCE_INTERVAL_SEC=300

# Compliance
# (no env required; endpoints work out of the box)
```

---

## 6. Quote 검증 (production verifier)

v4.4 는 **schema-only** 검증을 ship 합니다. Production deployment 는
다음 verifier 중 하나를 register 하세요:

### 6.1 Intel TDX (DCAP)

```bash
pip install intel-sgx-dcap-quote-verification-py
```

```python
from aegis.attest.tee_verifier import TEEQuoteVerifier
import sgx_quote_verification as dcap

def intel_dcap_verify(quote, expected):
    raw = bytes.fromhex(quote.raw_quote_hex)
    result = dcap.verify_quote(raw)  # uses Intel PCS automatically
    return VerificationResult(
        valid=result.is_valid,
        provider="tdx",
        reasons=tuple(result.advisory_ids),
        extras={"trust_level": "intel-pcs-verified", "tcb_status": result.tcb_status},
    )

verifier = TEEQuoteVerifier()
verifier.register_provider("tdx", intel_dcap_verify)
```

### 6.2 AMD SEV-SNP

```bash
pip install sev-snp-utils
```

```python
import sev_snp_utils as snp

def amd_kds_verify(quote, expected):
    raw = bytes.fromhex(quote.raw_quote_hex)
    result = snp.verify_attestation_report(raw)  # fetches AMD KDS certs
    return VerificationResult(
        valid=result.valid,
        provider="sev-snp",
        reasons=tuple(result.errors),
        extras={"trust_level": "amd-kds-verified", "tcb": result.reported_tcb},
    )

verifier.register_provider("sev-snp", amd_kds_verify)
```

---

## 7. TEE-sealed key (v4.5 milestone)

v4.4 는 sealed-key abstraction 만 ship 합니다. 실제 ioctl-based seal/unseal 은:

- **SEV-SNP**: `SNP_GET_DERIVED_KEY` 로 TCB-bound AES key 도출 → AES-GCM 으로 audit 키 wrap
- **TDX**: Linux 6.10+ Intel TPM bridge driver 필요 (currently in upstream review)
- **CCA**: ARM Realm Management Monitor seal API

설계가 끝났고 (`src/aegis/sign/sealed_key.py`), v4.5 에서 SEV-SNP path 가 먼저 land 합니다.

---

## 8. 배포 후 점검 체크리스트

```bash
# (a) TEE provider auto-detect 됐나?
curl http://aegis:8080/attestation/tee | jq '.provider'
# 기대: "tdx" 또는 "sev-snp" (NOT "mock")

# (b) HW collectors 가 실 데이터?
curl http://aegis:8080/attestation | jq '.tee_endpoint_ref'

# (c) Audit patrol 동작?
curl http://aegis:8080/audit/patrol/status | jq '.enabled'
# 기대: true

# (d) Compliance evidence 한 번 생성:
curl -X POST http://aegis:8080/compliance/evidence \
  -d '{"framework":"soc2","period_start_ns":0,"period_end_ns":1700000000000000000,"format":"markdown"}' \
  | head -50
```

모두 ✅ 나오면 production-ready.

---

## 9. Troubleshooting

### 9.1 `/dev/tdx_guest` 가 없음

- VM 이 Confidential VM 으로 launch 됐는지 확인
- Kernel 이 TDX guest 지원 (Linux 6.4+) 확인
- `dmesg | grep TDX` 출력 확인

### 9.2 ioctl returns ENOTTY

- Kernel 이 너무 오래됨 (`TDX_CMD_GET_REPORT0` 가 필요, Linux 6.4+)
- Code path 가 자동으로 mock 으로 fallback

### 9.3 quote verification fails with "schema-only"

- v4.4 default 동작. Production 은 Intel DCAP / AMD KDS verifier register 필수 (위 §6 참조)

### 9.4 Container 에서 `/dev/tdx_guest` 가 안 보임

- `--device /dev/tdx_guest:/dev/tdx_guest` 옵션 필요
- Kubernetes 의 경우: `securityContext.privileged: true` 또는 device plugin

---

## 10. 다음 milestone (v4.5+)

- AWS Nitro Enclave attestation
- ARM CCA 실 ioctl
- NVIDIA NAS GPU CC 통합
- TEE-sealed key 실 ioctl path (SEV-SNP 먼저)
- ML-DSA dual-sign (post-quantum, Claim 25)

---

**문서 끝.** 질문 / 피드백: GitHub Issues 또는 docs/PATENT_SUPPLEMENT_v3.md §3.18 참조.
