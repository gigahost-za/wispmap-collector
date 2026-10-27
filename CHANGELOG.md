# Changelog

Images: `ghcr.io/gigahost-za/wispmap-collector:<version>` for linux/amd64 and linux/arm64.
Update with `docker compose pull && docker compose up -d`.

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
