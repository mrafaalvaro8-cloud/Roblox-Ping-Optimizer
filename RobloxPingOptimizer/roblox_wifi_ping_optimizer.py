#!/usr/bin/env python3
"""
Roblox setup.roblox.com — Latency Optimizer  (CLI entry, merged v7.1)
=====================================================================
Thin CLI over optimizer_core: argparse, console formatting, and
re-exports of the core names test_optimizer.py imports.
Focus: keep setup.roblox.com under 50 ms wherever routing permits.

Safe by default:
  - Scans and monitors, never modifies your system.
  - Optional --apply-hosts writes ONLY the stable best edge to the hosts file.
  - --restore reverts the hosts file from the one-time backup.
  - Backup is created ONCE and never overwritten.

Honest limits:
  If setup.roblox.com is served from a far edge, no local tuning can fix it.
  The script tells you which hop adds the latency and which resolver returned
  which edge, so you know if it is ISP-side.

Windows only: uses ping -n, tracert, ipconfig and the hosts file. On any
other OS it prints a clear message and exits with status 2.

Usage:
  python roblox_wifi_ping_optimizer.py                 # 30 min scan + monitor
  python roblox_wifi_ping_optimizer.py -m 60           # 60 min
  python roblox_wifi_ping_optimizer.py --quick         # fast scan, 5 min monitor
  python roblox_wifi_ping_optimizer.py --apply-hosts   # + write hosts (needs admin)
  python roblox_wifi_ping_optimizer.py --restore       # revert hosts
  python roblox_wifi_ping_optimizer.py --scan-only     # just discover edges, exit
  python roblox_wifi_ping_optimizer.py --aggressive    # 30s priority re-eval
  python roblox_wifi_ping_optimizer.py --targets "setup.roblox.com,rbxgame.roblox.com"

Tests:  python -m unittest -v test_optimizer
"""

import argparse
import os
import sys

from optimizer_core import (  # noqa: F401  (re-exported: test_optimizer imports them here)
    DEFAULT_TARGETS,
    HOSTS_MARKER,
    PRIORITY_HOST,
    PING_INTERVAL_DEFAULT,
    RESCAN_AGGRESSIVE_SEC,
    RESCAN_DEFAULT_SEC,
    SESSION_MINUTES_DEFAULT,
    STABILITY_ROUNDS,
    SessionAborted,
    SessionConfig,
    _parse_doh_answers,
    build_hosts_lines,
    flush_dns,
    identify_bottleneck,
    jitter,
    log,
    parse_targets,
    parse_tracert_output,
    pct,
    prune_hosts_lines,
    restore_hosts,
    run,
    run_session,
    stability_gate,
)


def _configure_streams():
    """UTF-8 output keeps the check/warning glyphs from crashing on legacy
    code pages. Safe no-op on streams without reconfigure()."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

_configure_streams()

# ── Session events → console ───────────────────────────────────────────────
def print_event(kind, data, msg):
    """run_session event → console line. `msg` IS the original log line for
    that event, so output stays byte-identical to the pre-split script; `data`
    carries structured payloads for GUI frontends and is unused here."""
    if msg is not None:
        log(msg)

# ── Commands ───────────────────────────────────────────────────────────────
def cmd_restore():
    log("── Restore hosts file ──")
    if restore_hosts():
        flush_dns()
        log("✓ Done. Restart your browser/Roblox to pick up the change.")

def cmd_scan_only(targets, rounds):
    run_session(SessionConfig(targets=list(targets), rounds=rounds,
                              mode="scan_only"), emit=print_event)

def cmd_full_run(targets, minutes, interval, aggressive, apply_hosts, rounds):
    run_session(SessionConfig(targets=list(targets), minutes=minutes,
                              interval=interval, aggressive=aggressive,
                              apply_hosts=apply_hosts, rounds=rounds),
                emit=print_event)

# ── Argparse ───────────────────────────────────────────────────────────────
def build_parser():
    ap = argparse.ArgumentParser(
        description="Roblox setup.roblox.com latency optimizer (merged v7.1)")
    ap.add_argument("-m", "--minutes", type=int, default=None,
                    help=f"monitoring session length in minutes "
                         f"(default {SESSION_MINUTES_DEFAULT})")
    ap.add_argument("-i", "--interval", type=int, default=PING_INTERVAL_DEFAULT,
                    help=f"seconds between monitor samples "
                         f"(default {PING_INTERVAL_DEFAULT})")
    ap.add_argument("--quick", action="store_true",
                    help="fast run: 1 scan round + 5 min monitor "
                         "(explicit -m/--rounds still win)")
    ap.add_argument("--aggressive", action="store_true",
                    help=f"re-evaluate the priority host every "
                         f"{RESCAN_AGGRESSIVE_SEC}s instead of "
                         f"{RESCAN_DEFAULT_SEC}s while monitoring")
    ap.add_argument("--apply-hosts", action="store_true",
                    help="write stable best edges to the hosts file (needs admin)")
    ap.add_argument("--restore", action="store_true",
                    help="restore hosts from one-time backup and exit")
    ap.add_argument("--scan-only", action="store_true",
                    help="just scan edges and print results, no monitoring")
    ap.add_argument("--rounds", type=int, default=None,
                    help=f"scan rounds before stability gate "
                         f"(default {STABILITY_ROUNDS})")
    ap.add_argument("--targets", type=str, default="",
                    help="comma-separated hosts (default: setup.roblox.com + friends)")
    return ap

def resolve_session(args):
    """Apply --quick defaults without overriding explicit -m/--rounds."""
    minutes = args.minutes
    if minutes is None:
        minutes = 5 if args.quick else SESSION_MINUTES_DEFAULT
    rounds = args.rounds
    if rounds is None:
        rounds = 1 if args.quick else STABILITY_ROUNDS
    if minutes < 1:
        raise ValueError("--minutes must be >= 1")
    if rounds < 1:
        raise ValueError("--rounds must be >= 1")
    return minutes, rounds

def main():
    args = build_parser().parse_args()

    if os.name != "nt":
        log("✗ Windows only: this tool needs ping -n, tracert, ipconfig and "
            "the hosts file. Run it on Windows.")
        sys.exit(2)

    if args.restore:
        cmd_restore()
        return

    if args.targets:
        try:
            targets = parse_targets(args.targets)
        except ValueError as e:
            log(f"✗ {e}")
            sys.exit(2)
    else:
        targets = list(DEFAULT_TARGETS)

    try:
        minutes, rounds = resolve_session(args)
    except ValueError as e:
        log(f"✗ {e}")
        sys.exit(2)

    if args.interval < 1:
        log("✗ --interval must be >= 1 second")
        sys.exit(2)

    try:
        if args.scan_only:
            cmd_scan_only(targets, rounds)
        else:
            cmd_full_run(targets, minutes, args.interval,
                         args.aggressive, args.apply_hosts, rounds)
    except SessionAborted:
        sys.exit(1)

if __name__ == "__main__":
    main()
