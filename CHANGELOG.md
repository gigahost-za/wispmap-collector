# Changelog

Images: `ghcr.io/gigahost-za/wispmap-collector:<version>` for linux/amd64 and linux/arm64.
Update with `docker compose pull && docker compose up -d`.

## 0.5.0 (2026-10-31)

- Reads PoE-out on every poll: per port the mode (auto-on / forced-on / off), status (powered on, waiting for
  load, short circuit, overload, voltage too low, ...), voltage, current and power. Routers without PoE-out send
  none. Needs WISPMap with PoE support (for "PoE fault" alerts and the device page); older servers ignore it.
- `--capture` also records the live PoE readings (`/interface/ethernet/poe/monitor ... once`, still read-only).
- Not yet checked on real PoE hardware: the readings are built from MikroTik's manual. Please send a `--capture`
  of a PoE router if the numbers look wrong.

## 0.4.0 (2026-10-27)

- Reads `/system/health` on every poll: input voltage, board and CPU temperature, power draw, PSU and fan state.
  Works with both RouterOS 7 (a row per sensor) and RouterOS 6 (one row of fields); routers without sensors
  (CHR, x86) simply send none. Needs WISPMap with health support; older servers ignore it.
- `wispmap-collector --capture HOST`: prints what a router's API returns for the menus WISPMap reads (read-only,
  serial numbers removed), to share for support or test data:
  `docker compose run --rm collector wispmap-collector --capture 10.0.0.1 > router.json`

## 0.3.0 (2026-10-27)

Built for big networks: one collector on 4 vCPU polls about 1,000 routers a minute.

- Polls up to 64 routers at the same time (`WISPMAP_POLL_THREADS`, default 64), in its own thread pool, so slow
  or hung routers no longer hold up the rest. A 337-router cycle with 35 misbehaving routers dropped from
  32–47 s to 17–19 s.
- Raises its open-files limit to 65,536 at start (and `compose.yaml` sets the same `ulimits`). 1,000 routers
  used to fail with "Too many open files".
- Reads RouterOS API replies in 64 KB blocks instead of one small read per word: CPU per poll cycle down about
  5× (28–31 s → 4–6 s for 337 routers).
- Pings with `fping -i 1`: a 500-host round takes 5 s instead of 28 s.
- Sends results every 10 s by default (the server sets this; changeable per collector in WISPMap).
- `tools/fakeros.py` (a fake RouterOS API server: slow, hung, dropping and refusing routers, big routing tables)
  and `tools/scale_test.py` for testing at scale without real routers.

## 0.2.0 (2026-10-25)

First release as a Docker image.

- Server mode: fetches its device list and intervals from WISPMap, sends results over HTTPS, and keeps unsent
  results in an on-disk buffer (`/data`) through outages and restarts.
- RouterOS API polling (v6 and v7): system resources, interfaces and traffic, wireless, neighbours (every
  15 minutes), OSPF interfaces, neighbours and active routes per interface.
- Multi-arch image (amd64 + arm64), non-root, `compose.yaml` for installs.
