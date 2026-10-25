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
| 15 Runner | ⬜ | |
| 16 Break things | ⬜ | |

## Open questions

- ~~Does any LAN device announce itself on CHR-CORE `ether1`?~~ Yes — router + Windows host via LLDP (see /ip/neighbor).
- v6 behaviour of everything above — needs a v6 CHR or fixtures from a real v6 device.
