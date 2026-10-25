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
| 16 Break things | 🟡 | Done by Hermes (VirtualBox): L1 cable cut/restore, TWR-A power-cycle. Found + fixed: collector ignored SIGINT/SIGTERM when backgrounded → signal handlers in `run()` (65 tests). To do (needs admin on CHRs): L3 failover, password change |

## Open questions

- ~~Does any LAN device announce itself on CHR-CORE `ether1`?~~ Yes — router + Windows host via LLDP (see /ip/neighbor).
- v6 behaviour of everything above — needs a v6 CHR or fixtures from a real v6 device.
