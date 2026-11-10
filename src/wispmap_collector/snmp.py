"""SNMP for non-MikroTik devices (0.8.0): credential profiles from .env and a small async client (pysnmp).

Credentials stay on this machine, like the RouterOS login. A device names a credential profile; "default" is:

    SNMP_COMMUNITY=public                      # SNMP v2c
  or
    SNMP_V3_USER=wispmap                       # SNMP v3 (authPriv)
    SNMP_V3_AUTH_KEY=...    SNMP_V3_AUTH_PROTO=SHA      (SHA, SHA256, SHA512, MD5)
    SNMP_V3_PRIV_KEY=...    SNMP_V3_PRIV_PROTO=AES      (AES, AES256, DES; no PRIV_KEY = authNoPriv)

and any other profile adds its name: SNMP_COMMUNITY_TOWERS=..., SNMP_V3_USER_UPS=..., and so on.
"""
import os
from dataclasses import dataclass, field

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    bulk_walk_cmd,
    get_cmd,
    next_cmd,
    usmAesCfb128Protocol,
    usmAesCfb256Protocol,
    usmDESPrivProtocol,
    usmHMAC192SHA256AuthProtocol,
    usmHMAC384SHA512AuthProtocol,
    usmHMACMD5AuthProtocol,
    usmHMACSHAAuthProtocol,
)

AUTH = {"SHA": usmHMACSHAAuthProtocol, "SHA256": usmHMAC192SHA256AuthProtocol,
        "SHA512": usmHMAC384SHA512AuthProtocol, "MD5": usmHMACMD5AuthProtocol}
PRIV = {"AES": usmAesCfb128Protocol, "AES256": usmAesCfb256Protocol, "DES": usmDESPrivProtocol}
TIMEOUT_S, RETRIES = 2, 1
MAX_ROWS = 4000                      # a walk stops here, whatever the device says (a runaway table is not our job)
_EMPTY = {"NoSuchObject", "NoSuchInstance", "EndOfMibView"}
_AUTH_ERRORS = ("unknownUserName", "wrongDigest", "authenticationFailure", "unknownSecurityName",
                "decryptionError", "unsupportedSecLevel")


class SnmpTimeout(TimeoutError):
    """No answer (also what a wrong v2c community looks like: the device stays silent)."""


class SnmpAuthError(Exception):
    """SNMP v3 refused our user or keys."""


class SnmpError(Exception):
    """Any other SNMP failure; the message says what."""


@dataclass(frozen=True)
class SnmpProfile:
    version: str                      # "2c" or "3"
    community: str = field(default="", repr=False)
    user: str = ""
    auth_proto: str = "SHA"
    auth_key: str = field(default="", repr=False)
    priv_proto: str = "AES"
    priv_key: str = field(default="", repr=False)

    def auth_data(self):
        if self.version == "2c":
            return CommunityData(self.community, mpModel=1)
        kw = {}
        if self.auth_key:
            kw.update(authKey=self.auth_key, authProtocol=AUTH[self.auth_proto])
        if self.priv_key:
            kw.update(privKey=self.priv_key, privProtocol=PRIV[self.priv_proto])
        return UsmUserData(self.user, **kw)


def snmp_profile(name: str, env=None) -> SnmpProfile | None:
    """The named credential profile from the environment (.env), or None if it isn't set up."""
    env = os.environ if env is None else env
    suffix = "" if name in ("", "default") else "_" + name.upper().replace("-", "_")
    if env.get(f"SNMP_V3_USER{suffix}"):
        auth = (env.get(f"SNMP_V3_AUTH_PROTO{suffix}") or "SHA").upper()
        priv = (env.get(f"SNMP_V3_PRIV_PROTO{suffix}") or "AES").upper()
        if auth not in AUTH or priv not in PRIV:
            return None
        return SnmpProfile("3", user=env[f"SNMP_V3_USER{suffix}"], auth_proto=auth,
                           auth_key=env.get(f"SNMP_V3_AUTH_KEY{suffix}", ""), priv_proto=priv,
                           priv_key=env.get(f"SNMP_V3_PRIV_KEY{suffix}", ""))
    if env.get(f"SNMP_COMMUNITY{suffix}"):
        return SnmpProfile("2c", community=env[f"SNMP_COMMUNITY{suffix}"])
    return None


def convert(value):
    """pysnmp value -> plain Python: int for numbers (TimeTicks stay in hundredths of a second), bytes for octet
    strings (MACs and text alike; snmpdev decides), dotted text for IP addresses, None for "no such" answers."""
    kind = type(value).__name__
    if kind in _EMPTY:
        return None
    if kind == "IpAddress":
        return ".".join(str(b) for b in value.asOctets())
    if kind in ("ObjectIdentifier", "ObjectName"):
        return str(value)
    if hasattr(value, "asOctets"):
        return bytes(value.asOctets())
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _check(err, status, index, binds):
    if err:
        text = str(err)
        if "timeout" in text.lower() or type(err).__name__ == "RequestTimedOut":
            raise SnmpTimeout(text)
        if any(a in text for a in _AUTH_ERRORS):
            raise SnmpAuthError(text)
        raise SnmpError(text)
    if status:
        raise SnmpError(f"{status.prettyPrint()} at {binds[int(index) - 1][0] if index and binds else '?'}")


class SnmpClient:
    """One engine for the whole collector; a device is (host, port, profile). get/walk/first are coroutines."""

    def __init__(self, timeout_s: float = TIMEOUT_S, retries: int = RETRIES):
        self.engine = SnmpEngine()
        self.timeout_s, self.retries = timeout_s, retries

    async def _target(self, host, port):
        return await UdpTransportTarget.create((host, port), timeout=self.timeout_s, retries=self.retries)

    async def get(self, host: str, port: int, profile: SnmpProfile, oids) -> dict:
        """{oid: value} for the OIDs the device has (missing ones are left out)."""
        out = {}
        oids = list(oids)
        target = await self._target(host, port)
        for i in range(0, len(oids), 20):                       # small requests: some radios cap the PDU size
            r = await get_cmd(self.engine, profile.auth_data(), target, ContextData(),
                              *[ObjectType(ObjectIdentity(o)) for o in oids[i:i + 20]])
            _check(*r)
            for oid, value in r[3]:
                v = convert(value)
                if v is not None:
                    out[str(oid)] = v
        return out

    async def walk(self, host: str, port: int, profile: SnmpProfile, base: str, max_rows: int = MAX_ROWS) -> dict:
        """Every OID under base (GETBULK), {oid: value}."""
        out = {}
        target = await self._target(host, port)
        async for r in bulk_walk_cmd(self.engine, profile.auth_data(), target, ContextData(), 0, 25,
                                     ObjectType(ObjectIdentity(base)), lexicographicMode=False):
            _check(*r)
            for oid, value in r[3]:
                v = convert(value)
                if v is not None:
                    out[str(oid)] = v
            if len(out) >= max_rows:
                break
        return out

    async def present(self, host: str, port: int, profile: SnmpProfile, bases) -> set:
        """Which of these subtrees does the device have? One GETNEXT with every base in it."""
        bases = list(bases)
        target = await self._target(host, port)
        r = await next_cmd(self.engine, profile.auth_data(), target, ContextData(),
                           *[ObjectType(ObjectIdentity(b)) for b in bases], lexicographicMode=False)
        _check(*r)
        found = set()
        for base, (oid, value) in zip(bases, r[3]):
            if str(oid).startswith(base + ".") and convert(value) is not None:
                found.add(base)
        return found

    async def get_raw(self, host: str, port: int, profile: SnmpProfile, oids):
        """(oid, pysnmp value) pairs for the OIDs the device has (keeps the SNMP types)."""
        target = await self._target(host, port)
        r = await get_cmd(self.engine, profile.auth_data(), target, ContextData(),
                          *[ObjectType(ObjectIdentity(o)) for o in oids])
        _check(*r)
        return [(str(oid), value) for oid, value in r[3] if type(value).__name__ not in _EMPTY]

    async def walk_raw(self, host: str, port: int, profile: SnmpProfile, base: str, max_rows: int = 20000):
        """(oid, pysnmp value) pairs under base, for --capture-snmp (keeps the SNMP types)."""
        out = []
        target = await self._target(host, port)
        async for r in bulk_walk_cmd(self.engine, profile.auth_data(), target, ContextData(), 0, 25,
                                     ObjectType(ObjectIdentity(base)), lexicographicMode=False):
            _check(*r)
            out += [(str(oid), value) for oid, value in r[3] if type(value).__name__ not in _EMPTY]
            if len(out) >= max_rows:
                break
        return out

    def close(self) -> None:
        try:
            self.engine.close_dispatcher()
        except Exception:
            pass
