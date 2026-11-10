# WISPMap collector

The open-source agent that runs **inside your network** for [WISPMap](https://wispmap.co.za). It pings your
devices, polls MikroTik RouterOS (v6/v7) over the API, and sends the results out to your WISPMap server.

- **Outbound only.** It opens no ports. It connects to your routers (API 8728, ICMP) and to the WISPMap server (HTTPS).
- **Your router passwords stay here.** They live in this machine's `.env` and are never sent to the server.
- **Survives outages.** If the server can't be reached, results are buffered on disk and sent later.
- **Read-only.** It needs a RouterOS user with the `read,api,test` policies and nothing more.

## Install (Docker)

You need a small Linux machine or VM inside the network, with Docker. Any x86-64 or ARM64 box works, including
a Raspberry Pi 4/5.

The quickest way: in WISPMap go to **Admin → Collectors → Add collector** and run the one-line command it shows
(`curl -fsSL https://<your WISPMap>/collector/install.sh | sh -s -- <token>`). It does the steps below for you and
asks for the router password on your terminal. To do it by hand:

1. In WISPMap go to **Admin → Collectors → Add collector** and copy the token. It is shown only once.
2. On every router, add the read-only user:
   ```
   /user group add name=wispmon policy=read,api,test
   /user add name=wispmon group=wispmon password="<a long random password>"
   ```
3. On the collector machine:
   ```bash
   mkdir wispmap-collector && cd wispmap-collector
   curl -fsSLO https://raw.githubusercontent.com/gigahost-za/wispmap-collector/main/compose.yaml
   curl -fsSL  https://raw.githubusercontent.com/gigahost-za/wispmap-collector/main/.env.example -o .env
   chmod 600 .env
   nano .env                 # WISPMAP_URL, WISPMAP_TOKEN, WISPMON_USER, WISPMON_PASSWORD
   docker compose up -d
   docker compose logs -f    # expect "connected to … as a collector of '<your org>'" and "config v…: N devices"
   ```
   Within a minute, the collector shows **online** on WISPMap's Collectors page.

Update with `docker compose pull && docker compose up -d`; WISPMap's Collectors page says when a newer release
is out. To stay on one release line, set `COLLECTOR_TAG=0.3` in `.env` (gets 0.3.x fixes, never 0.4). What changed
in each release: [CHANGELOG.md](CHANGELOG.md).
Unsent results are kept in the `collector-data` volume across restarts and updates.

## Run from source (development)

```bash
uv sync
cp .env.example .env && chmod 600 .env    # fill it in
uv run wispmap-collector                  # server mode (WISPMAP_URL + WISPMAP_TOKEN set)
uv run wispmap-collector --local          # local mode: devices from devices.toml, results printed as JSON
uv run pytest -q -m "not lab"             # tests (the "lab" ones need real routers)
docker compose build                      # build the image from this checkout
```

## What it reads

| Every | What | RouterOS |
|---|---|---|
| 30 s | ping (loss, latency) | ICMP via `fping` |
| 60 s | uptime, CPU, memory, version | `/system/resource` |
| 60 s | interfaces: state, traffic, errors | `/interface` |
| 60 s | OSPF interfaces, neighbours, routes per interface | `/routing/ospf/*`, `/ip/route` (active OSPF routes only) |
| 60 s | voltage, temperatures, PSU and fans | `/system/health` |
| 60 s | PoE-out per port: power, voltage, faults | `/interface/ethernet/poe` (+ `monitor once`) |
| 60 s | wireless: access points and PtP links, each client's signal, SNR, CCQ, rates and uptime | `/interface/wireless`, `/interface/wifi` (or `wifiwave2`), `/interface/w60g` and their registration / station tables (+ `monitor once` on 60 GHz) |
| 5 min | subscribers online: counts only, no names or addresses | `/ppp/active`, `/ip/hotspot/active`, `/ip/dhcp-server/lease` |
| 5 min | security facts: services and who may reach them, a default `admin` user, how many full-rights users, this collector's own rights, update channel, open DNS, SOCKS, proxy, MAC Winbox. No user names or passwords | `/ip/service`, `/user`, `/user/group`, `/system/package/update`, `/ip/dns`, `/ip/socks`, `/ip/proxy`, `/tool/mac-server/mac-winbox` |
| 15 min | neighbours (to suggest links) | `/ip/neighbor` |

Everything is read with print commands (and `monitor once`), so the read-only group
`/user group add name=wispmon policy=read,api,test` is all it needs. Nothing on the router is changed.

### Other makes over SNMP (0.8.0)

Devices of kind `snmp` are read with SNMP GET/GETNEXT/GETBULK only (v2c or v3), every 60 s:

| What | MIB |
|---|---|
| uptime, name, interfaces: state, traffic (64-bit counters when present), errors | SNMPv2-MIB, IF-MIB, HOST-RESOURCES-MIB (CPU) |
| Ubiquiti airMAX (airOS 6/8): radio, each station's signal, SNR, CCQ, rates, connected time | UBNT-AirMAX-MIB |
| Ubiquiti airFiber (AF5/11/24...), LTU, AF60: link signal at both ends, capacity | UBNT-AirFIBER-MIB, UBNT-AFLTU-MIB, UI-AF60-MIB |
| Cambium ePMP: radio, each subscriber's signal and SNR both ways, quality | CAMBIUM-PMP80211-MIB |
| UPS: on battery, charge, runtime, battery voltage, load, input voltage, replace-battery | APC PowerNet, CyberPower CPS-MIB, UPS-MIB (RFC 1628) |

Credentials go in `.env` and stay there: `SNMP_COMMUNITY=...` (v2c), or `SNMP_V3_USER`, `SNMP_V3_AUTH_PROTO`
(SHA, SHA256, SHA512, MD5), `SNMP_V3_AUTH_KEY`, `SNMP_V3_PRIV_PROTO` (AES, AES256, DES), `SNMP_V3_PRIV_KEY`. A device
whose credential profile is `towers` uses `SNMP_COMMUNITY_TOWERS` (and so on). Prefer v3 where the device has it.

A device we don't know yet? `docker compose run --rm collector wispmap-collector --capture-snmp 10.0.0.20 > dev.snmprec`
writes what it answers, with names, addresses, MACs and serials replaced, so it can be added as a test fixture.

## Licence

Apache License 2.0. See `LICENSE`.
