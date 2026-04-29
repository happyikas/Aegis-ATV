"""Network counters via ``/proc/net/dev`` (no subprocess needed).

We avoid spawning ``ethtool`` because (a) it requires the binary to
be installed and (b) subprocess in the hot path adds 5-10 ms.
``/proc/net/dev`` gives us per-interface RX/TX byte counters directly.

The counter is **cumulative** across the host's lifetime. The
aggregator reads it as a delta: a per-call counter requires the
caller to subtract a baseline measurement. For the v4.1 first cut
we report the cumulative value and let M12 cost-divergence handle
the delta math.
"""

from __future__ import annotations

from pathlib import Path

from aegis.hw_telemetry.collectors.base import CollectorResult, HWCollector


class EthtoolCollector(HWCollector):
    name = "net"

    _PROC_NET_DEV = Path("/proc/net/dev")

    def is_available(self) -> bool:
        return self._PROC_NET_DEV.is_file()

    def collect(self) -> CollectorResult:
        if not self.is_available():
            return CollectorResult(available=False)
        try:
            with self._PROC_NET_DEV.open() as f:
                lines = f.readlines()
        except OSError:
            return CollectorResult(available=False)
        if len(lines) < 3:
            return CollectorResult(available=False)

        rx_total = 0
        tx_total = 0
        interfaces = []
        for line in lines[2:]:
            parts = line.split()
            if len(parts) < 17:
                continue
            iface = parts[0].rstrip(":")
            if iface == "lo":  # skip loopback
                continue
            try:
                rx = int(parts[1])
                tx = int(parts[9])
            except ValueError:
                continue
            rx_total += rx
            tx_total += tx
            interfaces.append({"iface": iface, "rx_bytes": rx, "tx_bytes": tx})

        return CollectorResult(
            available=True,
            values={
                "network_bytes_in": float(rx_total),
                "network_bytes_out": float(tx_total),
            },
            metadata={"interfaces": interfaces},
        )
