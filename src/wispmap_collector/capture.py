"""`wispmap-collector --capture HOST`: print what a router's API returns for the menus WISPMap reads, as JSON.

Read-only (print commands only), with the collector's own login from .env. Serial numbers and licence ids are
replaced, so the output can be shared to build test fixtures from real hardware:

    docker compose run --rm collector wispmap-collector --capture 10.0.0.1 > hap-ax2.json
"""
import json
import os
import sys

PATHS = [("system", "resource"), ("system", "health"), ("system", "routerboard"), ("interface",),
         ("interface", "ethernet", "poe"), ("system", "package")]
SCRUB = {"serial-number", "software-id", "license", "system-id", "factory-software", "mac-address", "address4",
         "address6", "address", "identity"}


def scrub(rows: list[dict]) -> list[dict]:
    return [{k: ("<removed>" if k in SCRUB else v) for k, v in r.items()} for r in rows]


def capture(api) -> dict:
    out = {}
    for path in PATHS:
        key = "_".join(path)
        try:
            out[key] = scrub(list(api.path(*path)))
        except Exception as exc:  # noqa: BLE001 - "no such command" etc. is the answer: record it
            out[key] = {"error": type(exc).__name__, "message": str(exc)[:300]}
    ports = out.get("interface_ethernet_poe")
    if isinstance(ports, list) and ports:              # live PoE readings: monitor <ids> once (still read-only)
        try:
            ids = ",".join(str(p.get(".id") or p.get("name")) for p in ports)
            out["interface_ethernet_poe_monitor"] = scrub(list(
                api.path("interface", "ethernet", "poe")("monitor", numbers=ids, once="")))
        except Exception as exc:  # noqa: BLE001
            out["interface_ethernet_poe_monitor"] = {"error": type(exc).__name__, "message": str(exc)[:300]}
    return out


def main(hosts: list[str]) -> int:
    import librouteros

    from .config import load_credentials
    username, password = load_credentials()
    port = int(os.environ.get("WISPMAP_CAPTURE_PORT", "8728"))
    result = {}
    for host in hosts:
        try:
            api = librouteros.connect(host=host, username=username, password=password, port=port, timeout=10)
        except Exception as exc:  # noqa: BLE001 - one unreachable router must not stop the others
            result[host] = {"error": type(exc).__name__, "message": str(exc)[:300]}
            continue
        try:
            result[host] = capture(api)
        finally:
            api.close()
    json.dump(result, sys.stdout, indent=1, default=str)
    print()
    return 0
