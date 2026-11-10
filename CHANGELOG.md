# Changelog

Images: `ghcr.io/gigahost-za/wispmap-collector:<version>` for linux/amd64 and linux/arm64.
Update with `docker compose pull && docker compose up -d`.

## 0.8.0 (2026-11-10)

- Other makes over SNMP (v2c or v3): devices of kind `snmp` get uptime, interfaces and traffic (64-bit counters when
  the device has them), read with GET/GETNEXT/GETBULK only, asynchronously beside the RouterOS polling.
- Ubiquiti airMAX, airFiber, LTU and AF60 and Cambium ePMP radios send their clients' signal, SNR and rates in the
  same shape as MikroTik radios; UPSs (APC, CyberPower, or any UPS with the standard UPS-MIB) send on battery,
  charge, runtime, battery voltage, load and whether the battery needs replacing.
- SNMP credentials live in `.env` (`SNMP_COMMUNITY`, or `SNMP_V3_*`; per profile with a `_<NAME>` suffix) and never
  leave this machine. A missing profile, a silent device (or wrong community) and a refused v3 login each say so.
- `--capture-snmp HOST` dumps what a device answers for WISPMap's MIBs as `.snmprec`, with names, addresses, MACs and
  serials replaced, to build fixtures from hardware we don't have.
- Built and tested against the vendors' MIBs and recordings of real devices (airOS 8, AF11, LTU, AF60, ePMP,
  APC, CyberPower) replayed in an SNMP simulator; not yet seen on a live Ubiquiti or Cambium network.
- Needs WISPMap 1.13 to show SNMP devices; older servers ignore the new fields. New dependency: pysnmp.

## 0.7.0 (2026-11-08)

- Reads BGP on every poll: one row per peer with its state (established, down, disabled), remote and local AS,
  uptime, when it last started and stopped, and how many routes it sent us (all, and in use). RouterOS 7
  (`/routing/bgp/session` + `connection`) and RouterOS 6 (`/routing/bgp/peer`). Routers without BGP send nothing.
- Routes per peer are counted by the router (`/ip/route print count-only`), so a full internet table costs one
  small answer per peer, and the count is right even when RouterOS's own prefix counter lags.
- Reads each router's active default route (gateway, interface, protocol): on a tower it shows which POP its traffic
  leaves through, so WISPMap can see traffic fail over from one POP to another.
- `--capture` also records the BGP menus.
- Fix (RouterOS 6): the count of routes per interface, which WISPMap uses to tell a link's role (active or
  backup), was always empty on v6 routers: v6 leaves fields out when asked for chosen columns. Found and
  checked on real RouterOS 6.49.23 routers; every read is now tested against real v6 captures.
- Needs WISPMap with BGP support to show any of it; older servers ignore it. Tested against a two-POP, two-upstream
  CHR lab (RouterOS 7.23); RouterOS 6 is built from MikroTik's manual.

## 0.6.0 (2026-11-04)

- Reads wireless on every poll: legacy `wireless`, the newer `wifi` (and `wifiwave2`) package and 60 GHz `w60g`.
  Per radio its mode, SSID, band, width and frequency; per client its signal, what it hears from us, SNR, CCQ or
  60 GHz quality, rates and uptime.
- Every 5 minutes: subscribers online (PPPoE, hotspot and DHCP counts only, never names) and the router's security
  facts for WISPMap's security check (services, users and groups, DNS, SOCKS/proxy, MAC Winbox, RouterOS update
  channel). Read-only, as always.
- `--capture` covers all of the above.

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
