"""Real TDX / SEV-SNP ioctl bindings (v4.4).

This module replaces the v3.x placeholder TDX/SEV-SNP code paths in
``aegis.attest.tee_quote``. When the corresponding device exists at
runtime, we issue the real attestation ioctl via ``ctypes`` and return
the raw quote bytes + parsed envelope. When the device is missing,
we return ``None`` so the caller can degrade gracefully (typically
back to the mock quote).

References
----------
* TDX:   ``include/uapi/linux/tdx-guest.h`` — ``TDX_CMD_GET_REPORT``
        produces a 1024-byte ``TDREPORT`` over an arbitrary 64-byte
        ``REPORTDATA``. Quote conversion (TDREPORT → TD-quote) is
        out of band via QGS over vsock; we surface the report and
        let the caller's verifier turn it into a quote.
* SEV-SNP: ``include/uapi/linux/sev-guest.h`` — ``SNP_GET_REPORT``
          produces an attestation report directly. No conversion
          step; the report **is** the verifiable artefact.

Why no third-party deps
-----------------------
The mainstream Python wrappers (``tdx-attest-rs`` Rust+Python binding,
``snpguest``) require build-time dependencies. We use ``ctypes`` +
the kernel UAPI structs so AegisData stays installable in any Python
environment. Production deployments may still install those wrappers
and patch this module to use them — the contract is identical.

Test coverage
-------------
We patch ``ctypes.CDLL`` and ``os.open``/``ioctl`` to simulate device
presence + responses, so the ioctl code path is unit-testable on any
host (including macOS / arm64 dev machines without the device files).
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import struct
from dataclasses import dataclass
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
# Kernel UAPI constants
# ─────────────────────────────────────────────────────────────────────

# Linux ioctl encoding helpers (matches asm-generic/ioctl.h)
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_DIRBITS = 2
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(direction: int, type_: int, nr: int, size: int) -> int:  # noqa: N802
    return (
        (direction << _IOC_DIRSHIFT)
        | (type_ << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def _IOWR(type_: int, nr: int, size: int) -> int:  # noqa: N802
    return _IOC(_IOC_READ | _IOC_WRITE, type_, nr, size)


# TDX guest device ioctl numbers (Linux UAPI)
# struct tdx_report_req { __u8 reportdata[64]; __u8 tdreport[1024]; }
TDX_DEVICE_PATH = "/dev/tdx_guest"
TDX_REPORT_DATA_LEN = 64
TDX_REPORT_LEN = 1024
_TDX_REQ_SIZE = TDX_REPORT_DATA_LEN + TDX_REPORT_LEN
TDX_CMD_GET_REPORT0 = _IOWR(ord("T"), 0x01, _TDX_REQ_SIZE)

# SEV-SNP guest device ioctl numbers
# struct snp_report_req {
#   __u8 user_data[64]; __u32 vmpl; __u8 rsvd[28];
# }
# struct snp_report_resp { __u8 data[4000]; }
SEV_DEVICE_PATH = "/dev/sev-guest"
SNP_USER_DATA_LEN = 64
SNP_REQ_RESERVED = 28
SNP_REPORT_LEN = 4000
_SNP_REQ_SIZE = SNP_USER_DATA_LEN + 4 + SNP_REQ_RESERVED + SNP_REPORT_LEN
SNP_GET_REPORT = _IOWR(ord("S"), 0x00, _SNP_REQ_SIZE)


# ─────────────────────────────────────────────────────────────────────
# Result envelopes
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TDXReport:
    """Parsed TDX TDREPORT (subset of the 1024-byte structure)."""

    raw: bytes
    # TDREPORT layout: 256-byte REPORTMACSTRUCT + 256-byte TEE_TCB_INFO +
    # 17-byte reserved + 512-byte TDINFO_STRUCT.
    # MRTD lives at offset 528 (TDINFO + 16) over 48 bytes.
    @property
    def mrtd(self) -> str:
        if len(self.raw) < 528 + 48:
            return ""
        return self.raw[528 : 528 + 48].hex()

    # The reportdata field we passed in is echoed back at offset 128.
    @property
    def report_data(self) -> str:
        if len(self.raw) < 128 + 64:
            return ""
        return self.raw[128 : 128 + 64].hex()


@dataclass(frozen=True)
class SEVSNPReport:
    """Parsed SEV-SNP attestation report (subset of the 1184-byte ABI)."""

    raw: bytes
    # SEV-SNP report layout (msg_report_resp.report at offset 32):
    #   bytes  0-15: VERSION + GUEST_SVN + POLICY
    #   bytes 80-143: REPORT_DATA
    #   bytes 144-191: MEASUREMENT (launch measurement)
    @property
    def measurement(self) -> str:
        # The "report" inside the response starts at offset 32.
        if len(self.raw) < 32 + 192:
            return ""
        return self.raw[32 + 144 : 32 + 192].hex()

    @property
    def report_data(self) -> str:
        if len(self.raw) < 32 + 144:
            return ""
        return self.raw[32 + 80 : 32 + 144].hex()


# ─────────────────────────────────────────────────────────────────────
# Public ioctl entrypoints
# ─────────────────────────────────────────────────────────────────────


def fetch_tdx_report(report_data: bytes) -> TDXReport | None:
    """Issue ``TDX_CMD_GET_REPORT0`` and return the 1024-byte TDREPORT.

    Returns ``None`` when ``/dev/tdx_guest`` is missing or any error
    occurs. Production deployments behind a TDX guest kernel get a
    real report; CI / dev hosts get None.
    """
    if len(report_data) != TDX_REPORT_DATA_LEN:
        raise ValueError(
            f"report_data must be exactly {TDX_REPORT_DATA_LEN} bytes, "
            f"got {len(report_data)}"
        )
    if not Path(TDX_DEVICE_PATH).exists():
        return None
    try:
        fd = os.open(TDX_DEVICE_PATH, os.O_RDWR)
    except OSError:
        return None
    try:
        # Build the request buffer: report_data || zeros(1024 for TDREPORT)
        buf = ctypes.create_string_buffer(_TDX_REQ_SIZE)
        ctypes.memmove(buf, report_data, TDX_REPORT_DATA_LEN)
        try:
            fcntl.ioctl(fd, TDX_CMD_GET_REPORT0, buf, True)
        except OSError as e:
            if e.errno == errno.ENOTTY:
                # Wrong ioctl number (kernel mismatch). Bail.
                return None
            raise
        report_bytes = bytes(buf.raw[TDX_REPORT_DATA_LEN : TDX_REPORT_DATA_LEN + TDX_REPORT_LEN])
        return TDXReport(raw=report_bytes)
    finally:
        os.close(fd)


def fetch_sev_snp_report(user_data: bytes, vmpl: int = 0) -> SEVSNPReport | None:
    """Issue ``SNP_GET_REPORT`` and return the attestation report.

    Returns ``None`` when ``/dev/sev-guest`` is missing or any error
    occurs. The 64-byte ``user_data`` is the report's caller-supplied
    nonce (analogous to TDX's REPORTDATA).
    """
    if len(user_data) != SNP_USER_DATA_LEN:
        raise ValueError(
            f"user_data must be exactly {SNP_USER_DATA_LEN} bytes, "
            f"got {len(user_data)}"
        )
    if not Path(SEV_DEVICE_PATH).exists():
        return None
    try:
        fd = os.open(SEV_DEVICE_PATH, os.O_RDWR)
    except OSError:
        return None
    try:
        # struct snp_report_req: user_data[64] || vmpl (u32) || rsvd[28]
        # We allocate a contiguous buffer big enough to hold both the
        # request and the response (kernel rewrites in-place).
        buf = ctypes.create_string_buffer(_SNP_REQ_SIZE)
        ctypes.memmove(buf, user_data, SNP_USER_DATA_LEN)
        struct.pack_into("<I", buf, SNP_USER_DATA_LEN, vmpl)
        try:
            fcntl.ioctl(fd, SNP_GET_REPORT, buf, True)
        except OSError as e:
            if e.errno == errno.ENOTTY:
                return None
            raise
        # Response sits at offset (user_data + vmpl + reserved).
        offset = SNP_USER_DATA_LEN + 4 + SNP_REQ_RESERVED
        report_bytes = bytes(buf.raw[offset : offset + SNP_REPORT_LEN])
        return SEVSNPReport(raw=report_bytes)
    finally:
        os.close(fd)


__all__ = [
    "SEVSNPReport",
    "SEV_DEVICE_PATH",
    "SNP_GET_REPORT",
    "SNP_REPORT_LEN",
    "SNP_USER_DATA_LEN",
    "TDX_CMD_GET_REPORT0",
    "TDX_DEVICE_PATH",
    "TDX_REPORT_DATA_LEN",
    "TDX_REPORT_LEN",
    "TDXReport",
    "fetch_sev_snp_report",
    "fetch_tdx_report",
]
