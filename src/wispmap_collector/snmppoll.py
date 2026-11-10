"""Polling SNMP devices (0.8.0). Async (pysnmp), next to the RouterOS thread pool; same poll result shape.

One poll: system group, then the device family (detected once, re-checked every few hours or after a failure),
then only the table columns that family needs. A UPS adds "ups"; radios add "wireless" in RouterOS's format.
"""
import asyncio
import time

from . import snmpdev
from .rates import RateCalculator
from .snmp import SnmpAuthError, SnmpClient, SnmpError, SnmpTimeout, snmp_profile

REDETECT_S = 6 * 3600
POLL_TIMEOUT_S = 45                   # one device, whole poll (a slow radio must not hold up the cycle)
CONCURRENCY = 32
_RATES = (("rx_byte", "rx_bps", 8), ("tx_byte", "tx_bps", 8), ("rx_packet", "rx_pps", 1), ("tx_packet", "tx_pps", 1),
          ("rx_error", "rx_err_ps", 1), ("tx_error", "tx_err_ps", 1), ("rx_drop", "rx_drop_ps", 1),
          ("tx_drop", "tx_drop_ps", 1))
IF_WALKS = [f"{snmpdev.IF}.{c}" for c in snmpdev.IF_COLUMNS] + [f"{snmpdev.IFX}.{c}" for c in snmpdev.IFX_COLUMNS]


def now_iso() -> str:
    from .runner import now_iso as iso
    return iso()


class SnmpPoller:
    def __init__(self, fast_s: int, client=None, clock=time.monotonic, env=None):
        self._client = client                    # created on first use: no SNMP devices, no SNMP engine
        self.clock = clock
        self.env = env
        self.rates = RateCalculator(max_gap_s=3 * fast_s)
        self.family: dict[str, tuple[str, float]] = {}    # device id -> (family, detected at)
        self.sem = asyncio.Semaphore(CONCURRENCY)

    @property
    def client(self):
        if self._client is None:
            self._client = SnmpClient()
        return self._client

    async def poll_all(self, devices) -> list[dict]:
        return list(await asyncio.gather(*(self.poll(d) for d in devices)))

    async def poll(self, d) -> dict:
        ts, t0 = now_iso(), self.clock()
        base = {"type": "poll", "device_id": d.id, "ts": ts, "cadence": "fast"}

        def failed(status, error):
            self.family.pop(d.id, None)          # detect again next time (the device may have been swapped)
            return {**base, "status": status, "error": error, "duration_ms": int((self.clock() - t0) * 1000)}

        profile = snmp_profile(d.credential_profile, self.env)
        if profile is None:
            name = d.credential_profile
            var = "SNMP_COMMUNITY" + ("" if name in ("", "default") else "_" + name.upper().replace("-", "_"))
            return failed("error", f"no SNMP credentials for profile {name!r} (set {var} or the SNMP_V3_* "
                                   f"variables in the collector's .env)")
        async with self.sem:
            try:
                return {**base, **await asyncio.wait_for(self._read(d, profile, t0), POLL_TIMEOUT_S)}
            except TimeoutError as exc:                 # SnmpTimeout, or the whole poll took too long
                hint = " (SNMP off, wrong community, or blocked?)" if profile.version == "2c" else ""
                return failed("timeout", f"no SNMP answer{hint}" if isinstance(exc, SnmpTimeout) or not str(exc)
                              else str(exc))
            except SnmpAuthError as exc:
                return failed("auth_failed", f"SNMP v3 login refused: {exc}")
            except (SnmpError, OSError) as exc:
                return failed("error", str(exc) or type(exc).__name__)
            except Exception as exc:              # one bad device must never stop the loop
                return failed("error", f"{type(exc).__name__}: {exc}")

    async def _read(self, d, profile, t0) -> dict:
        c, host, port = self.client, d.host, d.snmp_port
        data = await c.get(host, port, profile, snmpdev.SYS.values())
        if not data:
            raise SnmpError("the device answered but has no system group (sysDescr/sysUpTime)")
        family = await self._family(d, profile)
        for oid in IF_WALKS + snmpdev.WALKS[family]:
            data.update(await c.walk(host, port, profile, oid))
        if family in ("generic", "airmax", "epmp"):
            data.update(await c.walk(host, port, profile, snmpdev.HOST_RESOURCES_CPU))
        if family == "airmax":
            for oid in ("1.2.840.10036.3.1.2.1.3", "1.2.840.10036.3.1.2.1.4"):    # dot11 product name / version
                data.update(await c.walk(host, port, profile, oid))
        now = self.clock()
        raw_ifs = snmpdev.interfaces(data)
        ups = snmpdev.ups(data, family)
        return {"status": "ok", "error": None, "duration_ms": int((now - t0) * 1000),
                "system": snmpdev.system(data, family),
                "interfaces": [self._with_rates(d, i, now) for i in raw_ifs],
                "routing": None, "health": snmpdev.health(data, family, ups), "poe": None,
                "wireless": snmpdev.wireless(data, family, raw_ifs), "bgp": None,
                "snmp": {"family": family, "version": profile.version}, "ups": ups}

    async def _family(self, d, profile) -> str:
        known = self.family.get(d.id)
        if known and self.clock() - known[1] < REDETECT_S:
            return known[0]
        present = await self.client.present(d.host, d.snmp_port, profile, [oid for _, oid in snmpdev.FAMILIES])
        family = next((f for f, oid in snmpdev.FAMILIES if oid in present), "generic")
        self.family[d.id] = (family, self.clock())
        return family

    def _with_rates(self, d, i: dict, now: float) -> dict:
        out = {k: i[k] for k in ("name", "type", "mac", "running", "disabled", "link_downs")}
        for counter, field, mult in _RATES:
            r = self.rates.rate((d.id, i["name"], counter), now, i[counter])
            out[field] = None if r is None else round(r * mult, 2)
        return out

    def forget(self, keep_ids) -> None:
        """After a config reload: drop what we remember about devices that are gone."""
        for dev_id in list(self.family):
            if dev_id not in keep_ids:
                self.family.pop(dev_id)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
