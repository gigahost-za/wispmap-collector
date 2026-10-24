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

## librouteros gotchas

- Commands are **lazy generators**: nothing is sent until iterated → always `tuple(...)` / `list(...)`. An un-iterated `set` silently does nothing.
- `MultiTrapError` (router sent >1 `!trap`) is **not** a subclass of `TrapError` → catch both.
- Single trap example: `TrapError: not enough permissions (9)`.

## Security checks

- ✅ `wispmon` is read-only: `set` on /system/identity → `TrapError: not enough permissions (9)`.

## Design decisions from observations

1. Normalise MACs to lowercase with colons everywhere (link matching depends on it).
2. Feature probing: unsupported menus are cached per device, not reported as poll errors.
3. Neighbour `address` is a suggestion only; management IP is user-confirmed (prefer loopback).
4. Consider `software-id` as a secondary device identity (spec change candidate for `Device`).

## Progress

| Step | Status | Notes |
|---|---|---|
| 8 Project init | ✅ | uv project, librouteros + python-dotenv, pytest |
| 9 Explore API | ✅ (fixtures pending) | observations above; `tests/fixtures/chr/` is still empty and `tests/__init__.py` missing |
| 10 Parsing helpers | ⬜ | |
| 11 Normalise resource/interface | ⬜ | needs fixtures from Step 9 |
| 12 Rates | ⬜ | |
| 13 fping | ⬜ | |
| 14 Config | ⬜ | |
| 15 Runner | ⬜ | |
| 16 Break things | ⬜ | |

## Open questions

- Does any LAN device announce itself on CHR-CORE `ether1`? (`len(nb)` on CORE)
- v6 behaviour of everything above — needs a v6 CHR or fixtures from a real v6 device.
