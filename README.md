Roblox Ping Optimizer
Keeps setup.roblox.com under 50 ms wherever routing permits — by discovering every CDN edge each resolver can see, measuring them honestly, and (optionally) pinning the stable winner in the hosts file.

Two front-ends share one engine (optimizer_core.py):

Entry point	What it is
run_app.bat → optimizer_app.py	Desktop app (tkinter, stdlib only): live latency chart vs the 50 ms SLA, edges table, monitor stats, log, hosts panel
roblox_wifi_ping_optimizer.py	CLI — same behaviour, same output, all original flags
dist/RobloxPingOptimizer.exe	Standalone build (PyInstaller) — runs without Python installed
Quick start
bat
Copy
run_app.bat                       :: double-click; GUI, no admin needed
run_optimizer.bat                 :: elevated CLI: scan + lock best edge
run_roblox_wifi.bat               :: menu of CLI modes
python -m unittest -v test_optimizer   :: engine tests (55)
python -m unittest -v test_app         :: app + elevation tests (26)
The app
Scan now — discover + measure every edge for each target (read-only).
Start monitor — scan, then sample the pinned edges on an interval and chart them against the 50 ms SLA line until the session ends; a summary + report.json are always written, even when stopped.
Hosts tab — stability-gate verdict per host (agree across rounds, < 50 ms, jitter ≤ 15 ms), Apply stable edges, Restore backup. Settings and window size persist between launches.
Elevation
Scanning and monitoring never need Administrator. Apply/Restore run in-process when already elevated; otherwise the app relaunches only that step behind a UAC prompt. The child re-validates the mapping and reports back through elevated_result.json with a per-run nonce, so a stale result can never be trusted.

Safety semantics (unchanged)
Read-only by default; hosts writes happen only after the stability gate.
The hosts backup is created once and never overwritten; --restore (or the Restore button) reverts it.
Subprocesses are always argv lists; target/apply hostnames are strictly validated before anything external sees them.
If the closest edge is far away, the tool says so (with the bottleneck hop from tracert) instead of pretending local tuning can fix ISP routing.
CLI (all original flags)
bat
Copy
python roblox_wifi_ping_optimizer.py                 :: 30 min scan + monitor
python roblox_wifi_ping_optimizer.py --quick         :: 1 round, 5 min
python roblox_wifi_ping_optimizer.py --scan-only     :: discover edges only
python roblox_wifi_ping_optimizer.py --apply-hosts   :: + write hosts (admin)
python roblox_wifi_ping_optimizer.py --restore       :: revert hosts
python roblox_wifi_ping_optimizer.py -m 60 -i 10 --aggressive
python roblox_wifi_ping_optimizer.py --targets "setup.roblox.com,api.roblox.com"
Logs, summary.txt, report.json and the edge cache live in %LOCALAPPDATA%\roblox_setup_optimizer\.

Building the standalone .exe
bat
Copy
build_exe.bat      :: installs PyInstaller if needed, then builds
Output: dist\RobloxPingOptimizer.exe (--onefile --windowed — no console window, no Python required on the target machine). PyInstaller is a build-time-only dependency; the app itself stays stdlib-only.

Project layout
text
Copy
optimizer_core.py              engine: discovery, measurement, hosts, events
optimizer_app.py               tkinter desktop app (+ headless elevated child)
roblox_wifi_ping_optimizer.py  CLI entry point
test_optimizer.py              engine tests (unchanged from the script era)
test_app.py                    app / elevation / event-contract tests
run_app.bat                    GUI launcher
run_optimizer.bat              elevated CLI (scan + lock)
run_roblox_wifi.bat            CLI menu
build_exe.bat                  PyInstaller build
Honest limits
If setup.roblox.com resolves only to a far edge, no local tooling can fix that — the report tells you which hop adds the latency and which resolver returned which edge so you know it is ISP-side. CDN IPs rotate: re-scan (or re-apply) every 1–2 weeks, and restore the hosts file if Roblox misbehaves.
