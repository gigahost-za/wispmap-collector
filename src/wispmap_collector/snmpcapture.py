"""`wispmap-collector --capture-snmp HOST [--port 161] [--profile NAME]`: dump what a device answers over SNMP for
the subtrees WISPMap reads, as .snmprec lines (oid|type|value), so a device we haven't seen can become a test
fixture. Read-only (GET/GETNEXT/GETBULK). Names, contacts, locations, IP and MAC addresses and serial numbers are
replaced with stand-ins (MACs consistently, so table rows still line up):

    docker compose run --rm collector wispmap-collector --capture-snmp 10.0.0.20 > rocket-ac.snmprec
"""
import asyncio
import hashlib
import re
import sys

from .snmp import SnmpClient, snmp_profile

BASES = ["1.3.6.1.2.1.1", "1.3.6.1.2.1.2.2.1", "1.3.6.1.2.1.31.1.1.1", "1.3.6.1.2.1.25.3.3.1", "1.3.6.1.2.1.33.1",
         "1.2.840.10036.3.1.2.1", "1.3.6.1.4.1.41112.1", "1.3.6.1.4.1.17713.21", "1.3.6.1.4.1.318.1.1.1",
         "1.3.6.1.4.1.3808.1.1.1"]
# Text that identifies the operator or a customer: replaced outright
SCRUB_TEXT = ("1.3.6.1.2.1.1.4.", "1.3.6.1.2.1.1.5.", "1.3.6.1.2.1.1.6.",          # sysContact, sysName, sysLocation
              "1.3.6.1.2.1.31.1.1.1.18.",                                           # ifAlias (port descriptions)
              "1.3.6.1.4.1.41112.1.4.7.1.2.", "1.3.6.1.4.1.41112.1.4.5.1.2.",     # airMAX station name, SSID
              "1.3.6.1.4.1.41112.1.10.1.4.1.13.", "1.3.6.1.4.1.41112.1.10.1.2.5.",  # LTU remote name, SSID
              "1.3.6.1.4.1.41112.1.11.1.3.1.14.", "1.3.6.1.4.1.41112.1.11.1.1.4.",  # AF60 remote name, SSID
              "1.3.6.1.4.1.17713.21.1.2.30.1.18.", "1.3.6.1.4.1.17713.21.3.8.2.2.",  # ePMP SM host name, SSID
              "1.3.6.1.4.1.17713.21.1.1.11.", "1.3.6.1.4.1.17713.21.1.1.13.",       # ePMP SSID, device name
              "1.3.6.1.4.1.318.1.1.1.1.1.2.", "1.3.6.1.4.1.318.1.1.1.1.2.3.",       # APC UPS name, serial
              "1.3.6.1.4.1.3808.1.1.1.1.1.2.", "1.3.6.1.4.1.3808.1.1.1.1.2.3.",     # CyberPower UPS name, serial
              "1.3.6.1.2.1.33.1.1.5.")                                              # upsIdentName
MAC_INDEXED = ("1.3.6.1.4.1.41112.1.4.7.1.", "1.3.6.1.4.1.41112.1.10.1.4.1.", "1.3.6.1.4.1.41112.1.11.1.3.1.")
MAC_TEXT = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
TYPES = {"Integer32": 2, "Integer": 2, "OctetString": 4, "Null": 5, "ObjectIdentifier": 6, "IpAddress": 64,
         "Counter32": 65, "Gauge32": 66, "Unsigned32": 66, "TimeTicks": 67, "Opaque": 68, "Counter64": 70}


def fake_mac(octets: bytes) -> bytes:
    """Same real MAC -> same stand-in, in the locally administered range (02:...)."""
    return b"\x02" + hashlib.sha256(b"wispmap-capture" + octets).digest()[:5]


def scrub_index(oid: str) -> str:
    if not oid.startswith(MAC_INDEXED):
        return oid
    parts = oid.split(".")
    try:
        mac = bytes(int(p) for p in parts[-6:])
    except ValueError:
        return oid
    return ".".join(parts[:-6] + [str(b) for b in fake_mac(mac)])


def line(oid: str, value) -> str | None:
    kind = type(value).__name__
    tag = TYPES.get(kind)
    if tag is None:
        return None
    oid = scrub_index(oid)
    if tag == 64:
        return f"{oid}|64|192.0.2.{sum(value.asOctets()) % 250 + 1}"                # documentation range
    if tag == 4:
        raw = bytes(value.asOctets())
        if oid.startswith(SCRUB_TEXT):
            return f"{oid}|4|<removed>"
        if len(raw) == 6 and not raw.isascii() or (len(raw) == 6 and oid.startswith("1.3.6.1.2.1.2.2.1.6.")):
            return f"{oid}|4x|{fake_mac(raw).hex()}"
        text = raw.decode("ascii", "replace")
        if MAC_TEXT.match(text):
            sep = text[2]
            return f"{oid}|4|" + sep.join(f"{b:02x}" for b in fake_mac(bytes.fromhex(re.sub('[:-]', '', text))))
        if raw and (not raw.isascii() or any(b < 32 and b not in (9, 10, 13) for b in raw)):
            return f"{oid}|4x|{raw.hex()}"
        return f"{oid}|4|{text}"
    if tag == 6:
        return f"{oid}|6|{value}"
    return f"{oid}|{tag}|{int(value)}"


async def capture(host: str, port: int, profile_name: str, out=sys.stdout) -> int:
    profile = snmp_profile(profile_name)
    if profile is None:
        print(f"no SNMP credentials for profile {profile_name!r} in .env", file=sys.stderr)
        return 2
    client = SnmpClient(timeout_s=3, retries=2)
    rows = 0
    try:
        for base in BASES:
            rows_ = await client.walk_raw(host, port, profile, base)
            if not rows_ and base == "1.3.6.1.2.1.1":           # some agents skip the group on GETNEXT/GETBULK
                rows_ = await client.get_raw(host, port, profile, [f"1.3.6.1.2.1.1.{i}.0" for i in range(1, 8)])
            for oid, value in rows_:
                text = line(oid, value)
                if text:
                    print(text, file=out)
                    rows += 1
    except Exception as exc:
        print(f"capture failed after {rows} rows: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    print(f"{rows} rows from {host}", file=sys.stderr)
    return 0 if rows else 1


def main(argv: list[str]) -> int:
    args, port, profile = [], 161, "default"
    it = iter(argv)
    for a in it:
        if a == "--port":
            port = int(next(it, "161"))
        elif a == "--profile":
            profile = next(it, "default")
        elif not a.startswith("--"):
            args.append(a)
    if len(args) != 1:
        print("usage: wispmap-collector --capture-snmp HOST [--port 161] [--profile NAME]", file=sys.stderr)
        return 2
    return asyncio.run(capture(args[0], port, profile))
