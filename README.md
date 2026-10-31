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
| 15 min | neighbours (to suggest links) | `/ip/neighbor` |

## Licence

Apache License 2.0. See `LICENSE`.
