# Collector notes

What we learn from the real lab while building WISPMap's collector. Hermes keeps this file up to date as you report findings.
Spec references: `wispmap/docs/03-collector-protocol.md` (§7 RouterOS adapter), `04-status-and-alerts.md`.

## Lab

| Item | Value |
|---|---|
| Hypervisor | VirtualBox on Windows host (192.168.1.147) |
| LAN / gateway | 192.168.1.0/24 via **192.168.1.254** |
| Dev VM | `wispmap` 192.168.1.50 (Ubuntu 24.04, `enp0s3`, bridged) |
| RouterOS | 7.23.8 (long-term), CHR |
| CHR-CORE | ether1 LAN 192.168.1.201 · ether2 L1 10.0.1.1/30 · ether3 L3 10.0.3.1/30 (disabled) · lo 10.255.255.1 |
| CHR-TWR-A | ether1 L1 10.0.1.2/30 · ether2 L2 10.0.2.1/30 · lo 10.255.255.2 |
| CHR-TWR-B | ether1 L2 10.0.2.2/30 · ether2 L3 10.0.3.2/30 (disabled) · lo 10.255.255.3 |
| VirtualBox internal nets | `wm-l1`, `wm-l2`, `wm-l3` |
| Monitoring user | `wispmon` (group read,api,ssh), API restricted to 192.168.1.50 |

### Lab pitfalls hit (Part 1)

- Example gateway `192.168.1.1` was wrong → VM and CHR-CORE had no internet. Real gateway is `.254` (`route print -4` on Windows).
- VirtualBox default internal network name `intnet` put every link on one segment → OSPF stuck. Fixed with `wm-l1/2/3`; removed bridged LAN NIC from the towers.
- Copy-pasted OSPF `router-id` (TWR-A also had 10.255.255.1) → adjacency stuck in **ExStart**. Router-IDs must be unique.
- Addresses on disabled interfaces show flag `I` (invalid) — expected for L3.

## RouterOS observations (7.23.8, CHR)

### /system/resource

```python
{'uptime': '1h5m15s', 'version': '7.23.8 (long-term)', 'build-time': '2026-10-08 09:46:16',
 'factory-software': '7.1', 'free-memory': 850128896, 'total-memory': 1073741824, 'cpu': 'Intel(R)',
 'cpu-count': 1, 'cpu-frequency': 3599, 'cpu-load': 0, 'free-hdd-space': 72491008,
 'total-hdd-space': 93564928, 'write-sect-since-reboot': 336, 'write-sect-total': 336,
 'architecture-name': 'x86_64', 'board-name': 'CHR innotek GmbH VirtualBox', 'platform': 'MikroTik'}
```

- `uptime` uses unit format `1h5m15s` (no `hh:mm:ss` on 7.x; keep supporting it for v6).
- `version` = `7.23.8 (long-term)` → keep `7.23.8`; the bracket is the release channel.
- Numbers are already Python `int` (librouteros converts).
- `board-name` has the hypervisor appended → match CHR with `startswith("CHR")`.
- Memory used % = (total − free) / total × 100 → ~20.8 %.
- Worth keeping later: `architecture-name`, `cpu-count`, `free-hdd-space` (low flash alert), `write-sect-*` (flash wear on real RouterBOARDs).

### /interface

- Counters (`rx-byte`, `tx-byte`, …) are `int`; `running` / `disabled` are `bool`.
- MAC addresses are UPPERCASE → normalise to lowercase.
- Fixture (CHR-CORE): `ether1` 08:00:27:FC:D4:96 running, `ether2` 08:00:27:4A:3A:09 running, `ether3` 08:00:27:F6:C6:4D **not running** (L3 disabled), `lo`.
- `lo` is RouterOS 7's **built-in loopback** (`type: loopback`, MAC `00:00:00:00:00:00`) — no bridge needed on v7.
- All-zero MAC → `parse_mac` returns `None`; otherwise every loopback in the network would "match" every other one in link discovery.
- Extra keys seen: `actual-mtu`, `fp-rx-byte`/`fp-tx-byte` (fast-path counters), `tx-queue-drop`, `last-link-up-time`, `vrf`.

### /system/routerboard

- Does **not exist on CHR**: `MultiTrapError: no such command or directory (routerboard), no such command prefix`.
- → Design: "no such command" = feature unsupported (cache it), **not** a failed poll.

### /system/health

- Expected empty on CHR. Real voltage/temperature must come from fixtures captured on RouterBOARDs.

### /ip/neighbor (on CHR-CORE)

```python
{'.id': '*1', 'interface': 'ether2', 'address': '10.0.1.2', 'address4': '10.0.1.2',
 'address6': 'fe80::a00:27ff:fea1:3324', 'mac-address': '08:00:27:A1:33:24', 'identity': 'CHR-TWR-A',
 'platform': 'MikroTik', 'version': '7.23.8 (long-term) 2026-10-08 09:46:16', 'unpack': 'none',
 'age': '10s', 'uptime': '1h9m2s', 'software-id': 'yvfQt1O9j5A', 'board': 'CHR', 'ipv6': True,
 'interface-name': 'ether1', 'system-description': 'MikroTik RouterOS 7.23.8 (long-term) 2026-10-08 09:46:16 CHR',
 'system-caps': 'bridge,router', 'system-caps-enabled': 'router', 'discovered-by': 'cdp,lldp,mndp'}
```

- `interface` = **local** interface; `mac-address` = **remote** interface MAC; `interface-name` = remote interface name.
- MAC `08:00:27:A1:33:24` = TWR-A `ether1` → manual link match worked:
  `LINK: CHR-CORE ether2 <-> CHR-TWR-A ether1`
- `address` is the **link IP** of the announcing interface (10.0.1.2), not the loopback → only *suggest* it as mgmt IP when adopting.
- `version` here includes the build date → `parse_version` keeps the first word.
- `board` = `CHR` here, but `board-name` in /system/resource = `CHR innotek GmbH VirtualBox` → same device, different strings; normalise in one place.
- `software-id` is unique per install → candidate stable device identity (detect "same router, new IP"); licence-related, so anonymise in real-customer fixtures.
- `ipv6` is bool; `age` / `uptime` are duration strings.
- `interface` can be `ether2,bridge1` on bridged ports (not in this lab) → split on comma, take first.
- All three discovery protocols on by default (`cdp,lldp,mndp`).
- **LAN side (`ether1`) has 2 neighbours**, both via LLDP only, with empty `identity`/`platform`/`version` and a nonsense caps list (`bridge,wlan-ap,router,telephone,docsis-cable-device`):
  - `50:3D:7F:23:BE:10` = the ISP/LAN router (192.168.1.254)
  - `B4:2E:99:FE:61:DC` = the Windows host (192.168.1.147)
- → Confirms spec rule: >1 neighbour on one interface = shared segment/switch → **suggest only, never auto-link**.
- → Neighbours with empty identity/platform are low-confidence: show in "Discovered" only, never offer as RouterOS devices.

### /routing/ospf/neighbor (on CHR-CORE)

```python
{instance: default, area: backbone, interface: ether2, address: 10.0.1.2,
 router-id: 10.255.255.2, state: Full, state-changes: 5, adjacency: 40m20s,
 timeout: 40s, dynamic: True}
```

- `state` is capitalised (`Full`, `ExStart`) → normalise to lowercase.
- `state-changes` is a counter → a jump between polls = adjacency flapped (useful for `ospf_neighbor_down` / flap detection without catching the moment).
- `adjacency` is a duration → time since last (re)form; resetting = a flap we missed.

## Tooling gotchas

- Never use `sudo` inside the project/home dir: a root-owned `.gitignore` blocked VS Code saves (EACCES). Fix: `sudo chown gigahost:gigahost <file>`.
- VS Code Remote-SSH: extensions (Python/Pylance) must be installed **in SSH: wispmap**, otherwise no Testing panel.
- `.vscode/` is git-ignored (personal editor settings).

- Python name shadowing: a `def poll_interfaces` pasted into the test file silently replaced the imported one (NameError on `parse_mac`). Code goes in `src/`, tests only call it.

## fping observations (5.1)

- Results go to **stderr**; stdout is empty with `-q`.
- Host column is padded: `10.255.255.1  : 0.251 0.223 0.274` → split on " : " and strip.
- Lost packet = `-`; exit code 1 when any host is down (normal), 2 = name not resolved, ≥3 = real error.
- Ubuntu package ships with `cap_net_raw=ep` → works without root. In Docker: grant `CAP_NET_RAW`.
- Runtime ≈ count × interval + timeout (3 × 0.2 s + 1 s ≈ 1.5 s), regardless of host count.

## librouteros gotchas

- Commands are **lazy generators**: nothing is sent until iterated → always `tuple(...)` / `list(...)`. An un-iterated `set` silently does nothing.
- `MultiTrapError` (router sent >1 `!trap`) is **not** a subclass of `TrapError` → catch both.
- Single trap example: `TrapError: not enough permissions (9)`.

## python-dotenv gotchas

- `load_dotenv()` with no path calls `find_dotenv()`, which inspects the caller's file. Code piped via stdin (`python - <<EOF`) has no file → `AssertionError` in `find_dotenv`. Fix: `load_dotenv(".env")`. In the real collector pass an explicit path (env var `WISPMAP_ENV_FILE`, default `.env`) — no magic lookup in Docker.

## Security checks

- ✅ `wispmon` is read-only: `set` on /system/identity → `TrapError: not enough permissions (9)`.

## Runner observations (Step 15)

- First poll per device ≈ 145 ms (TCP connect + login), later polls ≈ 5 ms → persistent sessions matter (×30).
- First poll cycle: all rates `null` (no previous reading); second cycle has numbers.
- `ping_only` LAN gateway pinged every 30 s, never polled.
- Spec change candidates: PollResult status `unreachable` (EHOSTUNREACH/ENETUNREACH) added to the spec list; new line type `collector_error` for failures of the loop itself (e.g. `FpingError`).
- `asyncio.to_thread` uses the default thread pool (min(32, CPUs+4)); fine for the lab, revisit for 500+ devices (doc 03 §7 says 50 workers).

## Failure scenarios (Step 16, run 2026-10-25 07:48–07:56 UTC)

### L1 cut (CHR-CORE ether2 cable unplugged 07:49:56 → restored 07:52:27, 151 s)

| Signal | What the collector saw |
|---|---|
| Ping | TWR-A **and** TWR-B `recv 0` from 07:50:21 (first cycle after the cut, +25 s) to 07:52:22 → 5 lost cycles ≈ 150 s ✔ |
| Poll, 1st cycle | `timeout` after 5008 ms (existing session, command timed out) |
| Poll, later | `unreachable` after ~3 s (`[Errno 113] No route to host`: CORE's OSPF route gone → ICMP unreachable) |
| CHR-CORE itself | stayed `ok`; **ether2 `running=false`, `link_downs` 0 → 1** |
| Recovery | ping back 07:52:52 (+25 s); poll 07:53:22 `ok` 143 ms = automatic reconnect + new login |

Lessons:
- Same outage shows up as `timeout` then `unreachable` → alerting must treat timeout/unreachable/refused as one "API not reachable" class; ping + topology decide up/down.
- TWR-A and TWR-B look identical from the collector → root cause needs topology (reflection Q1).
- **The surviving end of a link reports it:** CORE ether2 `running=false` + `link_downs` +1 names the broken link directly. Strong root-cause signal → use in Part 8.
- Detection latency = up to one ping interval (30 s) + spec DOWN_AFTER (3 cycles) ≈ 90–120 s before an outage incident.

### TWR-A power-cycle (hard reset 07:54:07)

| Signal | What the collector saw |
|---|---|
| Ping | TWR-A **and** TWR-B lost for one cycle only (07:54:22); back 07:54:52 — CHR boots in < 45 s |
| Poll | 07:54:22 `timeout`; 07:55:22 `ok` with **uptime 70** (was 5028) → reboot detectable |
| Rates | TWR-A `rx_bps` null on the first poll after reboot (counter reset handled), numbers again next poll |
| Counters | TWR-A `link_downs` back to 0 (counters reset at boot) |

Lessons:
- With DOWN_AFTER = 3 ping cycles, this 30 s blip raises **no outage incident**, only a `device_rebooted` event (uptime decreased) — flap control working as designed.
- Reboot detection must compare uptime between polls, not rely on ping.

### L3 failover (user-run, 08:54–09:01 UTC)

| Time | Event | What the collector saw |
|---|---|---|
| ~08:55 | L3 enabled both ends | CORE ether3 + TWR-B ether2: `disabled` true → false, `running` → true, `link_downs` +1 |
| ~08:56 | L2 cut (TWR-A `/interface disable ether2`) | TWR-A ether2 `disabled=true running=false`; TWR-B ether1 rx → 0; TWR-B ether2 (L3) carries the traffic |
| 08:56–08:58 | L2 down | **TWR-B ping never lost a cycle (3/3 every 30 s), polls all `ok`** → OSPF rerouted faster than one ping interval |
| ~08:58 | restored (L2 on, L3 off) | traffic back on L2, L3 `disabled=true` again |

Lessons:
- Failover is invisible to ping/poll of the devices → the only evidence is **interface state** (`running`, `link_downs`) and traffic moving from one interface to another. WISPMap must alert on that ("backup path in use"), or nobody notices that the primary link is down.
- `disabled=true` (someone switched it off) vs `disabled=false, running=false` (cable/radio failure) → WISPMap can tell **admin action** from **fault**. Treat admin-disabled links as maintenance, not outage.
- Latency barely changed (TWR-B 0.5–0.9 ms on either path) → path changes can't be detected from RTT in a LAN-speed lab.

### Wrong password on TWR-B (user-run) — confirmed via the router's own log

TWR-B `/log` (readable by `wispmon`, times UTC):

```
08:54:56  user wispmon logged in from 192.168.1.50 via api          ← collector session starts
08:59:26  user wispmon changed by console:admin@tty1                 ← password set wrong
          (collector poll at 09:00:54 still "ok" — same session)
09:01:20  user wispmon changed by console:admin@tty1                 ← restored
--- retest ---
09:04:54  user wispmon logged in from 192.168.1.50 via api          ← collector restarted
09:05:31  user wispmon changed by console:admin@tty1                 ← password set wrong
09:05:47  user wispmon logged out from 192.168.1.50 via api         ← collector stopped
09:05:51  login failure for user wispmon from 192.168.1.50 via api  ← fresh login FAILS (system,error,critical)
09:06:10  user wispmon changed by console:admin@tty1                 ← restored
```

- **Confirmed: RouterOS keeps existing API sessions alive after a password change.** Only a new login fails.
- **Fix (done):** `Poller` renews every session after `SESSION_MAX_AGE_S` (6 h) → a changed password shows up as `auth_failed` within hours, not at the next outage. Tests: `test_session_renewed_after_max_age`, `test_renewal_surfaces_changed_password` (67 total).

### Bonus: /log is a goldmine (feature candidates)

- Config changes are logged **with who and what**: `device changed by console:admin@tty1 (/interface set ether2 disabled=yes)` → WISPMap can show "who changed what, when" next to an outage (change correlation / audit trail).
- `login failure for user … (system,error,critical)` → security alert for brute force / wrong credentials.
- `wispmon` (read policy) can read `/log` → no extra rights needed. Poll it incrementally (only new entries).

### Other observations

- Single latency spike at 07:56:22 (TWR-B 12.85 ms, gateway 7.17 ms, normally < 1 ms) = VM/host jitter → "degraded" must use averages over several cycles (spec: 5 cycles), never one sample.
- **Shutdown bug (fixed):** processes started in the background by a non-interactive shell inherit SIGINT = ignored, and SIGTERM (what `docker stop` sends) had no handler → collector could not be stopped cleanly. Fix: `loop.add_signal_handler` for SIGINT + SIGTERM → cancel main task → close sessions. Verified: SIGINT to python and SIGTERM via `uv run` both exit with "stopping".

### Reflection answers

1. **Root cause in the L1 cut:** both towers silent, but CORE is up and its ether2 went `running=false` → the break is the CORE–TWR-A link; TWR-B is "unreachable behind it". Topology (TWR-B is only reachable via TWR-A) + the surviving end's interface state give this without guessing.
2. **Timed-out polls hold threads:** each dead device holds a worker for 5 s (≈3 s when ICMP unreachable comes back). 500 devices / 50 threads with a region down → poll cycle stretches. Options: skip API polls for devices whose last ping cycle was 0/3, shorter connect timeout, bigger pool.
3. **Why rates on the collector:** it has the previous sample and the monotonic clock; buffering/replay later would make server-side deltas wrong; reboot resets are detected at the source.
4. **Alert on L2/L3 after failover?** Operators want to know a backup path is carrying traffic (capacity risk), but not at outage severity → `link_down` major/warning, outage only when something is unreachable.

## Design decisions from observations

1. Normalise MACs to lowercase with colons everywhere (link matching depends on it).
2. Feature probing: unsupported menus are cached per device, not reported as poll errors.
3. Neighbour `address` is a suggestion only; management IP is user-confirmed (prefer loopback).
4. Consider `software-id` as a secondary device identity (spec change candidate for `Device`).
5. Ignore all-zero MACs in link matching.
6. Normalise OSPF state to lowercase; use `state-changes` delta to detect flaps between polls.

## Progress

| Step | Status | Notes |
|---|---|---|
| 8 Project init | ✅ | uv project, librouteros + python-dotenv, pytest |
| 9 Explore API | ✅ | 4 fixtures in `tests/fixtures/chr/` (resource, interface ×4, ip_neighbor ×3, ospf_neighbor ×1); `tests/__init__.py` |
| 10 Parsing helpers | ✅ | `parse_mac` (zero MAC → None), `parse_version`, `parse_duration` (units + hh:mm:ss tail, ValueError on garbage); 21 tests. Live: `2h6m33s` → 7593 s, OSPF adjacency `1h26m25s` → 5185 s |
| 11 Normalise resource/interface | ✅ | `poll_system`, `poll_interfaces` + `FakeApi` (reads fixtures); 24 tests. Live on TWR-A/TWR-B: clean output, `lo` mac None, TWR-B ether2 (L3) running False |
| 12 Rates | ✅ | `RateCalculator`: None on first/missing/reset/gap/dt<=0; 7 tests. Live (ping 1400 B @0.2 s via CORE): ether1 rx ≈58 kbit/s (requests in from LAN), ether2 rx ≈52 kbit/s (replies from TWR-A) |
| 13 fping | ✅ | `build_fping_cmd`, `parse_fping`, async `run_ping` (+`FpingError` on exit ≥3); 8 tests incl. 1 `@lab`. Live: 4 hosts in 1.5 s; RTT grows per hop: CORE 0.22 ms, TWR-A 0.52, TWR-B 0.74; .99 recv 0 |
| 14 Config | ✅ | `devices.toml` (3 CHRs + ping-only LAN gateway) + `.env`; `load_config` validates kind/keys/dupes/creds → `ConfigError`; password `repr=False`; 11 tests (50 total). Live: all 4 reachable, uptimes ~100 s after lab restart |
| 15 Runner | ✅ | `runner.py`: `ping_cycle`, `Poller` (injectable connect/clock), `poll_cycle`, drift-free `every()`, `collector_error` events; `main()` with config errors + Ctrl+C; 14 tests (64 total). Live 70 s: 12 ping + 6 poll lines, all ok |
| 16 Break things | ✅ | L1 cut, TWR-A power-cycle (Hermes); L3 failover + password change (user). Fixed: graceful SIGINT/SIGTERM shutdown; periodic session renewal (6 h). 67 tests. **M0 complete** |

## Open questions

- ~~Does any LAN device announce itself on CHR-CORE `ether1`?~~ Yes — router + Windows host via LLDP (see /ip/neighbor).
- v6 behaviour of everything above — needs a v6 CHR or fixtures from a real v6 device.

## Server mode (Part 5b)

Run: `uv run wispmap-collector`. Server mode when `.env` has `WISPMAP_URL` + `WISPMAP_TOKEN`;
otherwise (or with `--local`) devices come from `devices.toml` and results print as JSON lines.
State lives in `WISPMAP_BUFFER_DIR` (default `./data`, git-ignored): `buffer.sqlite3` + `config.json`.

| Module | Job |
|---|---|
| `sync.py` | `POST /hello` at start + every 6 h (clock-skew warning > 30 s); `GET /config` every 60 s with ETag, or at once when an ingest reply shows a new `config_version`; caches the last good config |
| `uplink.py` | collects results, one gzipped batch per `push_s` → **disk first**, then sends: newest batch first, backlog oldest-first ≤ 1/s |
| `buffer.py` | SQLite queue (WAL, autocommit); caps 48 h / 200 MB, oldest dropped and counted |
| `server.py` | httpx client; token never in `repr` |

Status handling: 2xx/409 delete · 413 split in half · 400/422 drop (poison) · 401/403/426 pause 5 min · 429/5xx/network backoff 2 s → 5 min with jitter. Always keeps buffering.

Config reloads apply live: loops read `cfg.devices` / intervals every cycle; `Poller.sync()` closes sessions of removed devices or changed addresses. Only credential profile `default` exists so far (others → poll `error`).

### E2E (2026-10-25 10:23–10:25 UTC, intervals 10/20/10 s, separate server on :8001)

| Check | Result |
|---|---|
| Server down 46 s | collector logged "server unreachable" once, **4 batches buffered** |
| Recovery | "server reachable again; 5 batch(es) still queued"; **6 batches arrived within 10 s** |
| Data | 14 ping samples per CHR, **max gap 10 s**, 4 taken during the outage, **0 duplicates**; polls 8/8 ok |
| Live config change | LAN gateway ping disabled 10:25:04 → collector fetched v11, last ping 10:25:03 |
| SIGTERM | "stopping"; unsent tail (1 batch) left on disk for next start |

### Shell pitfall (test scripts)
- `( cd X && nohup prog & echo $! )` → `&` backgrounds the whole `cd && nohup` list, so `$!` is a **subshell** PID, not prog's. Killing it does nothing. Use `cd X; nohup prog & echo $!` (simple command), or find servers by socket: `ss -ltnpH "sport = :8001"`.
- `pkill -f "<pattern>"` over SSH also matches the SSH command line containing the pattern → kills your own shell.

### One-way OSPF stall (2026-10-25, seen by Part 6 live status)

| UTC | Router log |
|---|---|
| 10:32:13 | TWR-A: `admin` mac-telnet session (from CORE) logged out |
| 10:32:15–20 | TWR-A: `ether1 link down` → `link up` (5 s bounce on L1) |
| 10:32:30 | TWR-A: neighbor CORE back to **Full** |
| 10:32:51 | CORE: neighbor TWR-A **timeout → Down**, stayed Down (TWR-A still thought Full) |
| 10:32:40 → 10:43 | collector: TWR-A + TWR-B 100 % ping loss, polls `unreachable`; map red "no ping reply (3+ cycles)" |
| 10:43:33–46 | TWR-A's adjacencies to TWR-B and CORE reset and re-formed; all green again |

Lessons: a short link flap can leave OSPF half-up for minutes. Cable `running=true` on both ends, MNDP neighbor visible and the L1 /30 pingable while the loopbacks behind it are unreachable = routing problem, not a physical one. Worth a future rule: "interface running, neighbor seen, but OSPF neighbor not Full".
The collector process (started in a terminal) also stopped at 10:44:11; restarted detached with output in `data/collector.log`.


## Routing (OSPF) polling (Part 11)

- `src/wispmap_collector/routing.py` `poll_routing(api)`, called from the fast poll on the same session:
  `/routing/ospf/interface` (interface, cost, state, passive), `/routing/ospf/neighbor` (interface, address,
  router-id, state lower-cased, state-changes, adjacency → seconds), and active OSPF routes counted per outgoing
  interface (`immediate-gw` "10.0.1.2%ether2"; v6 `gateway-status` "… reachable via ether2").
- The route read is a server-side filtered query (`select(...).where(active, ospf)`): 3 ms on the lab CORE, and a
  router with a full BGP table never sends it. Counting stops at 50 000 routes.
- Poll result gains `"routing"`; None when the router can't tell (no menu / not allowed → TrapError, session kept)
  or the read failed (timeout → session dropped). A routing problem never fails the poll.
- Fixtures `tests/fixtures/chr/{routing_ospf_interface,routing_ospf_neighbor,ip_route}.json` from CHR-CORE 7.23.8.
  Disabled interfaces don't appear in /routing/ospf/interface (their template cost does, in interface-template).
- 108 tests.


## Packaging (Part 10a)

- `Dockerfile`: multi-stage. The uv 0.13.5 image runs `uv sync --frozen --no-dev --no-editable` into `/app/.venv`,
  then the runtime is `python:3.12-slim-trixie` with fping, user `wispmap` (uid 10001), `WORKDIR /data`,
  `VOLUME /data` (buffer.sqlite3 + config.json via `WISPMAP_BUFFER_DIR=/data`), and an exec-form ENTRYPOINT so
  the collector is PID 1 and gets SIGTERM. 185 MB. Only the installed package is in the image (no source, tests or .env).
- fping works as non-root without extra capabilities: Docker's default `net.ipv4.ping_group_range` allows
  unprivileged ICMP.
- `compose.yaml`: image `ghcr.io/gigahost-za/wispmap-collector:${COLLECTOR_TAG:-latest}` (+ `build: .`),
  `env_file: .env`, named volume `collector-data`, `restart: unless-stopped`, `stop_grace_period: 20s`, `hostname`
  (shown on the server), json-file log rotation 10m×3. Default bridge networking: the collector only makes outgoing connections.
- `.dockerignore` keeps `.env`, `data/`, `devices.toml`, `.git/` and tests out. `tests/test_packaging.py` checks
  this, plus non-root, exec entrypoint, `--frozen`, and that the uv versions agree with the workflow.
- `.github/workflows/image.yml`: tests, then a buildx build for amd64+arm64 (QEMU). A `v*` tag pushes to ghcr.io
  (`0.2.0`, `0.2`, `latest`, `sha-…`). **Not yet run.** The first tag push is the real test, and the package then
  has to be made public on GitHub (Packages → Package settings → Change visibility).
- Version 0.2.0 (routing). README written as the install guide.
- Pitfalls:
  - In a container, `WISPMAP_URL=http://127.0.0.1:8000` is the container itself. The lab `.env` now uses
    `http://192.168.1.50:8000`.
  - `pgrep -f`/`pkill -f` on the host also see container processes. The pattern `bin/wispmap-collector$` matches
    `/app/.venv/bin/wispmap-collector` inside the container. The old venv collector is matched as ` \.venv/bin/…`.
- Live check: the lab collector runs in the container. The server shows 0.2.0 and host `wispmap-collector`, with routing
  data arriving. `docker compose restart` stops it in 0.45 s, the unsent batch is kept in the volume and sent after
  the restart. 30 s of samples from the swap remain in the old `data/buffer.sqlite3` (gitignored, harmless).
- 113 tests.

## Scale (Phase B.2, 2026-10-26)

- `tools/fakeros.py`: a fake RouterOS API server. Hundreds of routers in one process on 127.x.y.z (all of 127/8 is
  loopback, so no setup). The second octet picks the behaviour: 21 normal, 22 slow (1-3 s per reply), 23 hung,
  24 drops the connection, 25 core router with 2,000 OSPF routes. A port nobody listens on = refused.
  `--latency-ms` simulates radio hops.
- `tools/scale_test.py`: the real Poller, poll/neighbour cycles and fping against it. It reports wall time, CPU,
  peak threads, statuses and RSS per cycle.
- Found and fixed (4-CPU VM):

  | Problem | Cause | Fix |
  |---|---|---|
  | 337 routers with 35 bad ones: 32-47 s per poll cycle (healthy alone: 3 s) | Python's default pool = cpu+4 = 8 threads; a hung router holds one for 5 s | own `ThreadPoolExecutor`, 64 threads (`WISPMAP_POLL_THREADS`, 1-512) → 17-19 s |
  | ~1,000 routers: crash "Too many open files" | one socket per router, soft limit 1,024 (host and Docker) | `raise_open_files()` at start (soft → 65,536, capped by hard) + compose `ulimits.nofile` |
  | 28-31 s CPU per cycle at 1,000 routers with 64 threads | librouteros does ~570 `recv()` per poll; every one hands off the GIL | `BufferedTransport` (64 KB reads, words cut by offset) via `connect_buffered` → 3.7-6 s CPU |
  | fping 500 hosts: 28 s (ping interval 30 s) | fping's default 10 ms between any two pings | `-i 1` → 5 s; fine non-root in the container |

- Result: 1,115 routers (1,000 healthy at 10 ms latency, 50 slow, 30 hung, 20 refused, 10 dropping, 5 core) poll in
  **17 s** per cycle, about 5 s CPU, 79 MB RSS; fping 1,215 hosts in 16 s. The poll interval is 60 s, so one
  collector on a small box handles about 1,000 routers. Past that, more threads or a second collector.
- Tests: `tests/test_scale.py` (the real librouteros client against fakeros, slow routers in parallel, thread setting,
  open-files, buffered reads). 122 tests.
- Later: the cycle is still bounded by the slowest router (each cycle waits for the 5-s timeouts). Polling each
  device on its own schedule would decouple them; not needed below ~1,000 routers.

## Release 0.3.0 (2026-10-27)

- v0.2.0 was tagged before the scale work, so the scale fixes ship as **0.3.0** (version in `pyproject.toml`,
  notes in `CHANGELOG.md`). The image reports the package version (`importlib.metadata`) to the server in
  `X-Wispmap-Collector`; the server marks older collectors "update available"
  (`WISPMAP_COLLECTOR_LATEST` on the server: bump it with every release).
- Releasing: commit + push `main`, then `git tag -a vX.Y.Z -m "..."` and `git push origin vX.Y.Z` (only that
  tag, not `--tags`). `.github/workflows/image.yml` tests, builds amd64 + arm64 and pushes
  `ghcr.io/gigahost-za/wispmap-collector:X.Y.Z`, `:X.Y` and `:latest`.
- The repository and the ghcr.io package must both be **public**: the README's install steps download
  `compose.yaml` from raw.githubusercontent.com, and installs pull the image without logging in.
- Check a release without logging in:
  `curl -s "https://ghcr.io/token?scope=repository:gigahost-za/wispmap-collector:pull"` gives a token, then
  `curl -s -H "Authorization: Bearer <token>" -H "Accept: application/vnd.oci.image.index.v1+json"
  https://ghcr.io/v2/gigahost-za/wispmap-collector/manifests/0.3.0` lists both platforms.

## compose.yaml without a fixed project name (2026-10-27)

- `name: wispmap-collector` is gone: Docker names the project after the folder. With the fixed name, a second
  collector on the same machine (another folder, another `.env`) replaced the first container and shared its
  buffer volume. Existing installs from the guide live in a folder called `wispmap-collector`, so their project
  and volume names don't change. No image change: compose.yaml is fetched from `main`.

## Health (0.4.0, 2026-10-27)

- `health.py`: `/system/health` read on every fast poll (same failure rule as routing: a trap -> None, the poll
  is still ok). Normalised to `voltage_v`, `temperature_c` (board: temperature / board-temperature1 /
  switch-temperature), `cpu_temp_c`, `power_w`, `states` (psuN, fan… from `*-state`) and `fans` (RPM).
  Fahrenheit (`type` F) converted; out-of-range values dropped; None when nothing usable.
- **Real CHR answer** (7.23.8, captured 2026-10-27): `[{"state": "disabled", "state-after-reboot": "enabled"}]`.
  Not empty as assumed earlier: it's the health *settings*. `/system/routerboard` and `/interface/ethernet/poe`
  answer "no such command (prefix)".
- v7 and v6 sensor fixtures (`tests/fixtures/docs-v7`, `docs-v6`) are written from the manual, **not
  captured**. Replace them with `--capture` output from real RouterBOARDs (owner is getting one).
- `--capture HOST`: read-only JSON of resource, health, routerboard, interface, PoE and packages; serials,
  licence ids, MACs and addresses replaced.

## PoE (0.5.0, 2026-10-31)

`poe.py`: `/interface/ethernet/poe` print (ports + mode), then `monitor numbers=<.id list> once=""` for the live
readings. Normalised: status/mode lowercase with underscores; voltage V, current mA (an "A" value x1000), power W,
units optional ("24.1V" or 24.1); out-of-range values -> None. No menu (CHR, boards without PoE-out) ->
TrapError -> `poe: None`; never fails the poll (same pattern as routing and health). `--capture` also records
the monitor output.

Not verified on hardware (no PoE in the CHR lab): fixtures in `tests/fixtures/docs-v7/interface_ethernet_poe*.json`
are from the manual. Unknowns to check with the first capture: whether the API sends units, and that
`once=""` is accepted (RouterOS API flag style).

Test fake: `FakeApi.path()` is iterable (print) and callable (`monitor`, recorded in `api.calls`); a missing
fixture raises TrapError like a router without that menu.
