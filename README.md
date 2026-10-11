# Roblox Ping Optimizer

A Windows tool that tries to keep `setup.roblox.com` under **50 ms**. It finds every CDN edge your DNS resolvers can see, measures each one, and can optionally pin the stable winner in your `hosts` file.

- **Windows only** (uses `ping`, `tracert`, `ipconfig` and the hosts file). On other systems it prints a message and exits.
- **Python 3, no extra packages** (standard library only).

## Quick start

All files live in the `RobloxPingOptimizer/` folder.

```bat
cd RobloxPingOptimizer

run_roblox_wifi.bat      :: menu: scan, monitor, apply, restore
run_optimizer.bat        :: admin: scan + lock the best edge in hosts
```

Or run the CLI directly:

```bat
python roblox_wifi_ping_optimizer.py                 :: 30 min scan + monitor
python roblox_wifi_ping_optimizer.py --quick         :: 1 scan round, 5 min monitor
python roblox_wifi_ping_optimizer.py --scan-only     :: discover and measure edges, then exit
python roblox_wifi_ping_optimizer.py --apply-hosts   :: also write hosts file (needs admin)
python roblox_wifi_ping_optimizer.py --restore       :: revert hosts from backup (needs admin)
python roblox_wifi_ping_optimizer.py -m 60 -i 10 --aggressive
python roblox_wifi_ping_optimizer.py --targets "setup.roblox.com,api.roblox.com"
```

| Flag | Meaning |
|---|---|
| `-m`, `--minutes` | Monitoring session length (default 30) |
| `-i`, `--interval` | Seconds between samples |
| `--quick` | 1 scan round + 5 minute monitor |
| `--rounds` | Scan rounds before the stability check (default 3) |
| `--aggressive` | Re-check the priority host more often |
| `--scan-only` | Discover edges only, no monitoring |
| `--apply-hosts` | Write stable best edges to the hosts file (admin) |
| `--restore` | Restore the hosts file from the one-time backup |
| `--targets` | Comma-separated hostnames to optimize |

## Safety

- Read-only by default. Scanning and monitoring never change your system.
- The hosts file is written only with `--apply-hosts`, and only after the stability check passes (same edge across rounds, under 50 ms, low jitter).
- The hosts backup is created once and never overwritten. `--restore` puts it back.
- Subprocesses are called with argument lists, and hostnames are validated before use.

## Honest limits

If `setup.roblox.com` only resolves to a far-away edge, no local tool can fix that. The report shows which hop adds the latency and which resolver returned which edge, so you can tell it is ISP-side. CDN IPs rotate, so re-scan (or re-apply) every 1-2 weeks, and run `--restore` if Roblox misbehaves.

Logs, `summary.txt`, `report.json` and the edge cache are written to `%LOCALAPPDATA%\roblox_setup_optimizer\`.

## Project layout

```text
RobloxPingOptimizer/
  optimizer_core.py               engine: discovery, measurement, hosts, session runner
  roblox_wifi_ping_optimizer.py   CLI entry point
  test_optimizer.py               engine tests
  run_roblox_wifi.bat             menu launcher
  run_optimizer.bat               admin launcher (scan + lock)
```

Run the tests from inside `RobloxPingOptimizer/`:

```bat
python -m unittest -v test_optimizer
```
