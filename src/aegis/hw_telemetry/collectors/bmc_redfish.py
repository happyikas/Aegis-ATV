"""BMC (Baseboard Management Controller) collector via Redfish.

Reads chassis power + thermal + fan from a Redfish endpoint, e.g.
``https://bmc.host:443/redfish/v1/Chassis/1/Thermal``. Out-of-band
relative to the OS so it survives kernel taint and provides a
secondary thermal measurement that can disagree with NVML if a
malicious kernel is hiding heat.

Authentication: opaque ``token`` string injected via
``AEGIS_BMC_REDFISH_TOKEN``. URL via ``AEGIS_BMC_REDFISH_URL``. If
either is missing, the collector reports UNAVAILABLE — production
deployments configure both via service-account secrets.

This implementation **does not** spawn a real HTTP request unless
the env vars are set. That keeps the unit tests hermetic.
"""

from __future__ import annotations

import os
from typing import Any

from aegis.hw_telemetry.collectors.base import CollectorResult, HWCollector


class BMCRedfishCollector(HWCollector):
    name = "bmc"

    _ENV_URL = "AEGIS_BMC_REDFISH_URL"
    _ENV_TOKEN = "AEGIS_BMC_REDFISH_TOKEN"

    def __init__(
        self, *, url: str | None = None, token: str | None = None,
        timeout_s: float = 1.0,
    ) -> None:
        self._url = url or os.environ.get(self._ENV_URL, "")
        self._token = token or os.environ.get(self._ENV_TOKEN, "")
        self._timeout_s = timeout_s

    def is_available(self) -> bool:
        return bool(self._url and self._token)

    def collect(self) -> CollectorResult:
        if not self.is_available():
            return CollectorResult(available=False)
        try:
            import urllib.request

            req = urllib.request.Request(
                url=f"{self._url}/redfish/v1/Chassis/1/Thermal",
                headers={"X-Auth-Token": self._token},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                import json
                data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 — every transport error → unavailable
            return CollectorResult(available=False)

        # Redfish thermal payload: list of "Temperatures" with ReadingCelsius.
        temps = data.get("Temperatures", [])
        max_c = 0.0
        for t in temps:
            reading = t.get("ReadingCelsius")
            if isinstance(reading, (int, float)):
                max_c = max(max_c, float(reading))
        return CollectorResult(
            available=True,
            values={
                # BMC thermal is a *secondary* signal; keep it under a
                # different key so it doesn't overwrite NVML's value.
                "_bmc_thermal_celsius_p95": max_c,
            },
            metadata={"temperatures_count": len(temps)},
        )
