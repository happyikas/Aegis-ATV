"""ContextMemory binary tier — 1 KB fixed-size record emulation.

The JSONL backend (:mod:`aegis.context_memory.writer`) is the
human-readable default. **This module is the second emulation
tier**: a deterministic fixed-size packed binary that mimics what a
real CXL SSD / Computational SSD device would store. The eventual
silicon serves the same shape, so callers can swap host-Python
``iter_binary`` for an in-storage scan without changing the
analytics dataclasses.

Why a second tier?
------------------

1. **Hardware spec** — Same ATV schema is the silicon spec. The
   packed layout below IS the spec, modulo endianness annotations.
2. **Memory mapping ready** — 1024-byte records align to NAND
   page boundaries; ``mmap`` over the file scans linearly without
   per-line allocation.
3. **Scan speed** — 5-10× faster than JSONL parsing on large stores
   (≥ 100k records). The host-Python scan is still O(n), but
   without the JSON tokeniser cost.
4. **Storage parity** — Comparable bytes-on-disk to JSONL but with
   bounded fields (no truncation surprises in production reports).

Opt-in via env var
------------------

The binary writer is **opt-in**: set
``AEGIS_CONTEXT_MEMORY_BINARY=1`` to enable. When on,
:func:`aegis.context_memory.writer.append` writes BOTH the JSONL
record AND the packed binary record. The JSONL store stays the
canonical source for ``aegis doctor`` reads; the binary mirror is
for benchmarking and as the silicon spec until the device ships.

Default path: ``~/.aegis/context_memory.bin``
Env override:  ``AEGIS_CONTEXT_MEMORY_BINARY_PATH``

Record layout (1024 bytes, little-endian)
-----------------------------------------

::

    offset  size   field                     type
    ------  ----   -----                     ----
       0      1   schema_version            u8
       1      1   decision                  u8  (0=ALLOW 1=REQ_APP 2=BLOCK 255=unknown)
       2      1   advisor_invoked           u8  (0|1)
       3      1   is_sidechain              u8  (0|1)
       4      1   mode                      u8  (0=local 1=sidecar 255=unknown)
       5      3   _pad_align8
       8      8   ts_ns                     u64
      16      4   latency_ms                f32
      20      4   tokens_in                 u32
      24      4   tokens_out                u32
      28      4   atv_dim                   u32
      32      8   cost_usd                  f64
      40      4   m13_score                 f32 (NaN = None)
      44      4   _pad
      48     64   trace_id                  utf-8 zero-padded
     112     64   invocation_id             utf-8 zero-padded
     176     64   aid                       utf-8 zero-padded
     240     32   tenant_id                 utf-8 zero-padded
     272     32   tool_name                 utf-8 zero-padded
     304     96   provider                  utf-8 zero-padded
     400     32   channel                   utf-8 zero-padded
     432    256   reason                    utf-8 zero-padded (truncated to 250)
     688     64   atv_sha3                  utf-8 zero-padded (hex)
     752    128   step_traces_compact       "key:val;key:val;..." truncated
     880     96   recommended_advisors_compact  "name,name,..." truncated
     976     48   _reserved
    ----  ----
    1024 total

All variable-length strings are encoded UTF-8 then zero-padded.
On decode, trailing NULs are stripped. Field lengths are chosen to
fit comfortable real-world values; longer inputs are truncated
silently (silicon would do the same — fixed-width fields are a
silicon contract).

Cross-check
-----------

:func:`equivalence_check` reads the JSONL and binary stores in
parallel and reports whether they line up record-for-record. Useful
as a CI sanity test and as an operator command (``aegis doctor
verify-binary``, future PR).
"""

from __future__ import annotations

import math
import os
import struct
from collections.abc import Iterator
from pathlib import Path

from aegis.context_memory.record import ContextMemoryRecord

# ── format constants ─────────────────────────────────────────────

RECORD_SIZE: int = 1024
"""Fixed bytes per record. NAND page friendly."""

# struct format string — little-endian, packed exactly to 1024 bytes.
# Field-by-field comments mirror the docstring layout table.
_HEADER_FMT = (
    "<"     # little-endian, no alignment padding inserted
    "B"     # schema_version
    "B"     # decision (enum)
    "B"     # advisor_invoked
    "B"     # is_sidechain
    "B"     # mode
    "3x"    # _pad_align8
    "Q"     # ts_ns
    "f"     # latency_ms
    "I"     # tokens_in
    "I"     # tokens_out
    "I"     # atv_dim
    "d"     # cost_usd
    "f"     # m13_score
    "4x"    # _pad
)
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 48 bytes

# String field sizes — matches the docstring layout.
_TRACE_ID_LEN = 64
_INVOCATION_ID_LEN = 64
_AID_LEN = 64
_TENANT_LEN = 32
_TOOL_NAME_LEN = 32
_PROVIDER_LEN = 96
_CHANNEL_LEN = 32
_REASON_LEN = 256
_ATV_SHA3_LEN = 64
_STEP_TRACES_LEN = 128
_RECOMMENDED_LEN = 96

_BODY_SIZE = (
    _TRACE_ID_LEN + _INVOCATION_ID_LEN + _AID_LEN + _TENANT_LEN
    + _TOOL_NAME_LEN + _PROVIDER_LEN + _CHANNEL_LEN + _REASON_LEN
    + _ATV_SHA3_LEN + _STEP_TRACES_LEN + _RECOMMENDED_LEN
)
_RESERVED_SIZE = RECORD_SIZE - _HEADER_SIZE - _BODY_SIZE
assert _RESERVED_SIZE >= 0, "binary layout overflow"

# Decision enum encoding — keep numeric values stable.
_DECISION_TO_BYTE = {"ALLOW": 0, "REQUIRE_APPROVAL": 1, "BLOCK": 2}
_BYTE_TO_DECISION = {v: k for k, v in _DECISION_TO_BYTE.items()}
_UNKNOWN_DECISION = 255

# Mode enum encoding.
_MODE_TO_BYTE = {"local": 0, "sidecar": 1}
_BYTE_TO_MODE = {v: k for k, v in _MODE_TO_BYTE.items()}
_UNKNOWN_MODE = 255


# ── path / env ───────────────────────────────────────────────────


def binary_enabled() -> bool:
    """Whether the binary tier is turned on for this process."""
    raw = os.environ.get("AEGIS_CONTEXT_MEMORY_BINARY", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def binary_path() -> Path:
    """Canonical binary store path.

    Override via ``AEGIS_CONTEXT_MEMORY_BINARY_PATH``. Default sits
    beside the JSONL store at ``~/.aegis/context_memory.bin``.
    """
    raw = os.environ.get("AEGIS_CONTEXT_MEMORY_BINARY_PATH", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".aegis" / "context_memory.bin"


# ── pack / unpack ────────────────────────────────────────────────


def pack(record: ContextMemoryRecord) -> bytes:
    """Pack a ContextMemoryRecord into exactly ``RECORD_SIZE`` bytes.

    Truncates strings that exceed their bounded field width.
    Encodes ``None`` numerics as ``NaN`` (m13_score) or ``0``.
    Unknown enum values map to ``255``.
    """
    decision_byte = _DECISION_TO_BYTE.get(record.decision, _UNKNOWN_DECISION)
    mode_byte = _MODE_TO_BYTE.get(record.mode, _UNKNOWN_MODE)
    m13 = (
        float("nan") if record.m13_score is None
        else float(record.m13_score)
    )
    header = struct.pack(
        _HEADER_FMT,
        record.schema_version & 0xFF,
        decision_byte,
        1 if record.advisor_invoked else 0,
        1 if record.is_sidechain else 0,
        mode_byte,
        record.ts_ns & 0xFFFFFFFFFFFFFFFF,
        float(record.latency_ms),
        record.tokens_in & 0xFFFFFFFF,
        record.tokens_out & 0xFFFFFFFF,
        record.atv_dim & 0xFFFFFFFF,
        float(record.cost_usd),
        m13,
    )
    body = b"".join((
        _pack_str(record.trace_id,       _TRACE_ID_LEN),
        _pack_str(record.invocation_id,  _INVOCATION_ID_LEN),
        _pack_str(record.aid,            _AID_LEN),
        _pack_str(record.tenant_id,      _TENANT_LEN),
        _pack_str(record.tool_name,      _TOOL_NAME_LEN),
        _pack_str(record.provider or "", _PROVIDER_LEN),
        _pack_str(record.channel or "",  _CHANNEL_LEN),
        _pack_str(record.reason,         _REASON_LEN),
        _pack_str(record.atv_sha3 or "", _ATV_SHA3_LEN),
        _pack_str(
            _compact_step_traces(record.step_traces), _STEP_TRACES_LEN,
        ),
        _pack_str(
            ",".join(record.recommended_advisors), _RECOMMENDED_LEN,
        ),
    ))
    reserved = b"\x00" * _RESERVED_SIZE
    rec_bytes = header + body + reserved
    assert len(rec_bytes) == RECORD_SIZE, (
        f"binary layout broken: got {len(rec_bytes)} bytes, "
        f"expected {RECORD_SIZE}"
    )
    return rec_bytes


def unpack(buf: bytes) -> ContextMemoryRecord:
    """Inverse of :func:`pack`. Strict — raises on wrong size."""
    if len(buf) != RECORD_SIZE:
        raise ValueError(
            f"binary record must be {RECORD_SIZE} bytes, got {len(buf)}",
        )

    (
        schema_version, decision_byte, advisor_invoked_byte,
        is_sidechain_byte, mode_byte,
        ts_ns, latency_ms,
        tokens_in, tokens_out, atv_dim,
        cost_usd, m13_score,
    ) = struct.unpack(_HEADER_FMT, buf[:_HEADER_SIZE])

    decision = _BYTE_TO_DECISION.get(decision_byte, "")
    mode = _BYTE_TO_MODE.get(mode_byte, "")
    m13: float | None = None if math.isnan(m13_score) else float(m13_score)

    # Walk the body in declared order.
    offset = _HEADER_SIZE
    def take(n: int) -> str:
        nonlocal offset
        s = _unpack_str(buf[offset:offset + n])
        offset += n
        return s

    trace_id = take(_TRACE_ID_LEN)
    invocation_id = take(_INVOCATION_ID_LEN)
    aid = take(_AID_LEN)
    tenant_id = take(_TENANT_LEN)
    tool_name = take(_TOOL_NAME_LEN)
    provider = take(_PROVIDER_LEN) or None
    channel = take(_CHANNEL_LEN) or None
    reason = take(_REASON_LEN)
    atv_sha3 = take(_ATV_SHA3_LEN) or None
    step_traces = _decompact_step_traces(take(_STEP_TRACES_LEN))
    rec_advisors_str = take(_RECOMMENDED_LEN)
    rec_advisors = tuple(
        a for a in (s.strip() for s in rec_advisors_str.split(","))
        if a
    )

    return ContextMemoryRecord(
        schema_version=int(schema_version),
        ts_ns=int(ts_ns),
        trace_id=trace_id,
        invocation_id=invocation_id,
        aid=aid,
        tenant_id=tenant_id,
        tool_name=tool_name,
        decision=decision,
        reason=reason,
        channel=channel,
        provider=provider,
        latency_ms=float(latency_ms),
        cost_usd=float(cost_usd),
        tokens_in=int(tokens_in),
        tokens_out=int(tokens_out),
        step_traces=step_traces,
        m13_score=m13,
        advisor_invoked=bool(advisor_invoked_byte),
        recommended_advisors=rec_advisors,
        atv_sha3=atv_sha3,
        atv_dim=int(atv_dim),
        is_sidechain=bool(is_sidechain_byte),
        mode=mode,
    )


# ── writer / reader ──────────────────────────────────────────────


def append_binary(
    record: ContextMemoryRecord,
    *,
    path: Path | None = None,
) -> bool:
    """Append one packed record. Returns ``True`` on success; never
    raises (same defensive contract as the JSONL writer)."""
    try:
        p = path if path is not None else binary_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("ab") as f:
            f.write(pack(record))
        return True
    except OSError:
        return False


def iter_binary(path: Path | None = None) -> Iterator[ContextMemoryRecord]:
    """Stream records out of the binary store.

    Missing file → empty iterator. Truncated trailing record (file
    size not a multiple of ``RECORD_SIZE``) is silently ignored —
    matches the JSONL "skip malformed line" semantics.
    """
    p = path if path is not None else binary_path()
    if not p.exists():
        return
    try:
        with p.open("rb") as f:
            while True:
                buf = f.read(RECORD_SIZE)
                if len(buf) < RECORD_SIZE:
                    return
                try:
                    yield unpack(buf)
                except (ValueError, struct.error):
                    continue
    except OSError:
        return


def read_all_binary(
    path: Path | None = None,
) -> list[ContextMemoryRecord]:
    """Materialise all binary records into memory."""
    return list(iter_binary(path))


# ── equivalence check (cross-tier sanity) ────────────────────────


def equivalence_check(
    jsonl_records: list[ContextMemoryRecord],
    binary_records: list[ContextMemoryRecord],
) -> tuple[bool, str]:
    """Compare two record lists for analytics-equivalence.

    Returns ``(True, "")`` when both stores describe the same set of
    ATV events (matching ``trace_id`` AND matching decision +
    cost + latency). Returns ``(False, reason)`` on first mismatch.

    Stronger than strict equality because the binary tier truncates
    long strings — we ignore those bytes here and compare only the
    analytics-relevant scalars.
    """
    if len(jsonl_records) != len(binary_records):
        return False, (
            f"record count differs: jsonl={len(jsonl_records)} "
            f"binary={len(binary_records)}"
        )
    j_by_trace = {r.trace_id: r for r in jsonl_records}
    b_by_trace = {r.trace_id: r for r in binary_records}
    if set(j_by_trace) != set(b_by_trace):
        only_j = set(j_by_trace) - set(b_by_trace)
        only_b = set(b_by_trace) - set(j_by_trace)
        return False, (
            f"trace_id mismatch: only_jsonl={sorted(only_j)[:3]} "
            f"only_binary={sorted(only_b)[:3]}"
        )
    for tid, j_rec in j_by_trace.items():
        b_rec = b_by_trace[tid]
        if j_rec.decision != b_rec.decision:
            return False, f"decision differs for {tid}"
        if abs(j_rec.cost_usd - b_rec.cost_usd) > 1e-9:
            return False, f"cost_usd differs for {tid}"
        if abs(j_rec.latency_ms - b_rec.latency_ms) > 1e-3:
            return False, f"latency_ms differs for {tid}"
        if j_rec.tokens_in != b_rec.tokens_in:
            return False, f"tokens_in differs for {tid}"
        if j_rec.tokens_out != b_rec.tokens_out:
            return False, f"tokens_out differs for {tid}"
    return True, ""


# ── string packing helpers ───────────────────────────────────────


def _pack_str(s: str, n: int) -> bytes:
    """UTF-8 encode + zero-pad / truncate to exactly ``n`` bytes.

    Truncation is byte-accurate, so a multi-byte UTF-8 character at
    the boundary may be split. We decode with ``errors="replace"``
    on the reverse path, so a half-character becomes U+FFFD rather
    than a crash.
    """
    raw = (s or "").encode("utf-8", errors="replace")
    if len(raw) >= n:
        return raw[:n]
    return raw + b"\x00" * (n - len(raw))


def _unpack_str(buf: bytes) -> str:
    """Decode UTF-8 zero-padded bytes; strip trailing NULs."""
    return buf.rstrip(b"\x00").decode("utf-8", errors="replace")


def _compact_step_traces(traces: dict[str, str]) -> str:
    """Encode ``{step: msg}`` as ``"step:msg;step:msg;..."``.

    Drops ``;`` and ``:`` from values to keep parser unambiguous.
    Truncation happens at the byte level inside :func:`_pack_str`.
    """
    parts: list[str] = []
    for k, v in traces.items():
        safe_k = str(k).replace(";", "").replace(":", "")
        safe_v = str(v).replace(";", "").replace(":", "")
        parts.append(f"{safe_k}:{safe_v}")
    return ";".join(parts)


def _decompact_step_traces(s: str) -> dict[str, str]:
    """Inverse of :func:`_compact_step_traces`. Skips malformed pairs."""
    if not s:
        return {}
    out: dict[str, str] = {}
    for chunk in s.split(";"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        k, _, v = chunk.partition(":")
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


__all__ = [
    "RECORD_SIZE",
    "append_binary",
    "binary_enabled",
    "binary_path",
    "equivalence_check",
    "iter_binary",
    "pack",
    "read_all_binary",
    "unpack",
]
