#!/usr/bin/env python3
"""
Roblox setup.roblox.com — Latency Optimizer: core engine
========================================================
Pure measurement / discovery / hosts / reporting logic plus the
event-emitting session runner shared by the CLI (roblox_wifi_ping_optimizer)
and the GUI (optimizer_app).

Presentation happens through two hooks:

  set_log_sink(fn)         every human-readable line goes through log(); the
                           default sink prints "[HH:MM:SS] msg" to stdout, a
                           frontend installs one that enqueues for its log
                           view instead.
  run_session(cfg, emit)   structured events: emit(kind, data, msg).
                           When msg is not None it IS the console line for
                           that event and is never also sent through the log
                           sink — a frontend appends event messages and sink
                           lines to its log view exactly once each.

Safety semantics (unchanged from the original script): scan/monitor is
read-only by default; the hosts file is written only after the stability
gate passes; the backup is created once and never overwritten; subprocesses
are always argv lists and hostnames are strictly validated.

Windows only: uses ping -n, tracert, ipconfig and the hosts file.
"""

import csv
import ctypes
import datetime
import ipaddress
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

SESSION_MINUTES_DEFAULT = 30
PING_INTERVAL_DEFAULT   = 5
SLA_MS                  = 50          # target for setup.roblox.com
STABILITY_ROUNDS        = 3           # scan rounds before locking hosts
STABILITY_MAX_JITTER    = 15          # ms — edge must be this steady
RESCAN_DEFAULT_SEC      = 60          # priority-host re-eval cadence
RESCAN_AGGRESSIVE_SEC   = 30          # ... with --aggressive

LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", "."),
                        "roblox_setup_optimizer")
LOG_FILE      = os.path.join(LOG_DIR, "ping_log.csv")
SUMMARY_FILE  = os.path.join(LOG_DIR, "summary.txt")
REPORT_FILE   = os.path.join(LOG_DIR, "report.json")
CACHE_FILE    = os.path.join(LOG_DIR, "edge_cache.json")

HOSTS_PATH    = r"C:\Windows\System32\drivers\etc\hosts"
HOSTS_BACKUP  = r"C:\Windows\System32\drivers\etc\hosts.roblox_backup"
HOSTS_MARKER  = "# Roblox Setup Optimizer"

# Hostnames accepted from --targets. Deliberately strict: these strings are
# handed to external programs, so shell metacharacters must never get through.
HOSTNAME_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

# ── Logging / event hooks ─────────────────────────────────────────────────
def ts():
    return datetime.datetime.now().strftime("%H:%M:%S")

def _default_log_sink(msg):
    print(f"[{ts()}] {msg}", flush=True)

_LOG_SINK = None

def set_log_sink(fn):
    """Install a log sink callable(msg); pass None to restore the default.
    Frontends call this once at startup so every log() line reaches them."""
    global _LOG_SINK
    _LOG_SINK = fn

def log(msg):
    (_LOG_SINK or _default_log_sink)(msg)

def _emit_null(kind, data, msg):
    """Default run_session emitter: structured events go nowhere."""
    return None

class SessionAborted(Exception):
    """Unrecoverable setup failure inside run_session (e.g. no log dir)."""

# ── Helpers ───────────────────────────────────────────────────────────────
def run(cmd, timeout=15):
    """Run an argv LIST (never a shell string). Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, shell=False, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except OSError as e:
        return -1, "", str(e)

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        rc, _, _ = run(["net", "session"], timeout=5)
        return rc == 0

def ensure_log_dir():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        return True
    except OSError as e:
        log(f"    ✗ Cannot create log dir {LOG_DIR}: {e}")
        return False

def valid_host(host):
    """True for a plain hostname or an IP literal. Rejects shell metacharacters."""
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return bool(HOSTNAME_RE.match(host))

def parse_targets(spec):
    """Parse --targets 'host[:port],host2' into [(host, port, label), ...].

    Raises ValueError on anything questionable so bad input fails fast here
    instead of reaching a subprocess later.
    """
    targets = []
    for raw in spec.split(","):
        raw = raw.strip().rstrip(".")
        if not raw:
            continue
        host, port = raw, 443
        if raw.count(":") == 1:            # host:port — never an IPv6 literal
            host, p = raw.rsplit(":", 1)
            if not p.isdigit():
                raise ValueError(f"invalid port in --targets entry: {raw!r}")
            port = int(p)
        if not valid_host(host):
            raise ValueError(
                f"invalid host in --targets entry: {raw!r} "
                f"(letters, digits, dots and hyphens only)")
        if not 1 <= port <= 65535:
            raise ValueError(f"port out of range in --targets entry: {raw!r}")
        targets.append((host, port, host))
    if not targets:
        raise ValueError("--targets did not contain any hosts")
    return targets

# ── Targets ────────────────────────────────────────────────────────────────
# setup.roblox.com is the priority. Others are context.
DEFAULT_TARGETS = [
    ("setup.roblox.com",                 443, "PRIORITY"),
    ("rbxgame.roblox.com",               443, "game server"),
    ("assetgame.roblox.com",             443, "assets"),
    ("api.roblox.com",                   443, "api"),
    ("versioncompatibility.api.roblox.com", 443, "version api"),
    ("www.roblox.com",                   443, "web"),
]

PRIORITY_HOST = "setup.roblox.com"

# Public DoH resolvers — different resolvers reveal different CDN edges
DOH_RESOLVERS = [
    ("Cloudflare", "https://cloudflare-dns.com/dns-query", "application/dns-json"),
    ("Google",     "https://dns.google/resolve",           None),
    ("Quad9",      "https://dns.quad9.net:5053/dns-query",  "application/dns-json"),
]

# ── Resolvers ──────────────────────────────────────────────────────────────
def resolve_system(host):
    """All IPv4 from the system resolver."""
    ips = []
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None,
                                                         socket.AF_UNSPEC):
            if fam == socket.AF_INET:
                ips.append(sockaddr[0])
    except socket.gaierror:
        pass
    return list(dict.fromkeys(ips))

def resolve_system_v6(host):
    ips = []
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None,
                                                         socket.AF_INET6):
            ips.append(sockaddr[0].split("%")[0])
    except socket.gaierror:
        pass
    return list(dict.fromkeys(ips))

def _doh(url, timeout=4):
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/dns-json",
                          "User-Agent": "roblox-setup-optimizer/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

def _parse_doh_answers(data, want):
    """Extract clean A (want=1) / AAAA (want=28) addresses from a DoH reply."""
    ips = []
    for ans in ((data or {}).get("Answer") or []):
        if ans.get("type") != want:
            continue
        d = (ans.get("data") or "").strip()
        if want == 1:
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", d):
                ips.append(d)
        elif ":" in d:
            ips.append(d.split("%")[0])
    return ips

def doh_resolve(host):
    """Query Cloudflare + Google + Quad9 for A and AAAA. Returns (v4, v6)."""
    v4, v6 = {}, {}   # ip -> set of resolver names
    for name, base, _accept in DOH_RESOLVERS:
        for qtype, want in [("A", 1), ("AAAA", 28)]:
            data = _doh(f"{base}?name={host}&type={qtype}")
            if not data:
                continue
            for ip in _parse_doh_answers(data, want):
                if want == 1:
                    v4.setdefault(ip, set()).add(name)
                else:
                    v6.setdefault(ip, set()).add(name)
    return v4, v6

# ── Latency measurement (ICMP then TCP 443 fallback) ───────────────────────
def ping_ip(ip, count=1, timeout_ms=1500):
    flag = "-6" if ":" in ip else "-4"
    rc, out, err = run(["ping", flag, "-n", str(count),
                        "-w", str(timeout_ms), ip])
    if rc != 0:
        return None
    m = re.search(r"time[=<](\d+)ms", out, re.IGNORECASE)
    return int(m.group(1)) if m else None

def tcp_ping(ip, port=443, timeout_sec=1.5):
    start = time.time()
    try:
        if ":" in ip:  # IPv6
            s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s.settimeout(timeout_sec)
            s.connect((ip, port, 0, 0))
        else:
            s = socket.create_connection((ip, port), timeout=timeout_sec)
        ms = int((time.time() - start) * 1000)
        s.close()
        return ms
    except Exception:
        return None

def measure(ip, port=443):
    """ICMP first, then TCP 443. setup.roblox.com blocks ICMP — TCP is essential."""
    ms = ping_ip(ip, count=1, timeout_ms=1200)
    if ms is not None:
        return ms, "ICMP"
    ms = tcp_ping(ip, port=port, timeout_sec=1.5)
    if ms is not None:
        return ms, "TCP"
    return None, "N/A"

def measure_median(ip, port=443, n=5):
    """Return (median_ms, jitter_ms_range, method) or (None, None, None)."""
    samples, method = [], "N/A"
    for _ in range(n):
        ms, m = measure(ip, port)
        if ms is not None:
            samples.append(ms)
            method = m
        time.sleep(0.12)
    if not samples:
        return None, None, None
    s = sorted(samples)
    median = s[len(s)//2]
    jitter = (max(s) - min(s)) if len(s) > 1 else 0
    return median, jitter, method

# ── Parallel edge scan ─────────────────────────────────────────────────────
def scan_edges(candidates, port=443, workers=16, samples=3):
    """Returns {ip: {"median":ms, "jitter":ms, "method":str}} for reachable IPs."""
    if not candidates:
        return {}
    results = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(candidates))) as ex:
        futs = {ex.submit(measure_median, ip, port, samples): ip
                for ip in candidates}
        for fut in as_completed(futs):
            ip = futs[fut]
            try:
                med, jit, meth = fut.result()
            except Exception:
                continue
            if med is not None:
                results[ip] = {"median": med, "jitter": jit, "method": meth}
    return results

# ── Cache ──────────────────────────────────────────────────────────────────
_cache = {}

def load_cache():
    global _cache
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            _cache = json.load(f).get("hosts", {}) or {}
    except Exception:
        _cache = {}

def save_cache():
    if not ensure_log_dir():
        return
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"hosts": _cache, "saved_at": time.time()}, f, indent=2)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass

# ── Hosts file ─────────────────────────────────────────────────────────────
def backup_hosts_once():
    """Create hosts backup ONCE. Never overwrite an existing backup."""
    if os.path.exists(HOSTS_BACKUP):
        log(f"    ✓ Backup already exists (never overwritten): {HOSTS_BACKUP}")
        return True
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8",
                  errors="surrogateescape") as src, \
             open(HOSTS_BACKUP, "w", encoding="utf-8",
                  errors="surrogateescape") as dst:
            dst.write(src.read())
        log(f"    ✓ Created one-time hosts backup: {HOSTS_BACKUP}")
        return True
    except Exception as e:
        log(f"    ✗ Could not back up hosts: {e}")
        return False

def _line_targets(line, domains):
    """True if a hosts line maps one of `domains` (comments never count)."""
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    return any(re.match(rf"^\S+\s+{re.escape(d)}(\s|$)", s) for d in domains)

def prune_hosts_lines(lines, domains, marker=HOSTS_MARKER):
    """Drop our marker block and stale entries for `domains`; keep the rest."""
    keep = [l for l in lines if marker not in l]
    return [l for l in keep if not _line_targets(l, domains)]

def build_hosts_lines(existing_lines, mapping, marker=HOSTS_MARKER):
    """Full new hosts line list: pruned existing lines + our managed block."""
    keep = prune_hosts_lines(existing_lines, set(mapping), marker)
    keep.append("")
    keep.append(f"{marker} Start")
    for dom, ip in mapping.items():
        keep.append(f"{ip}\t{dom}\t{marker}")
    keep.append(f"{marker} End")
    return keep

def write_hosts(mapping):
    """Write Roblox entries to hosts. Only called after stability gate passes."""
    if not is_admin():
        log("    ✗ Not admin — hosts file needs elevation.")
        return False
    if not backup_hosts_once():
        return False
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8",
                  errors="surrogateescape") as f:
            lines = f.read().splitlines()
        keep = build_hosts_lines(lines, mapping)
        with open(HOSTS_PATH, "w", encoding="utf-8",
                  errors="surrogateescape") as f:
            f.write("\n".join(keep) + "\n")
        log(f"    ✓ Wrote {len(mapping)} entr(ies) to hosts.")
        return True
    except Exception as e:
        log(f"    ✗ Failed to write hosts: {e}")
        return False

def restore_hosts():
    if not os.path.exists(HOSTS_BACKUP):
        log(f"    ✗ No backup found at {HOSTS_BACKUP}")
        return False
    if not is_admin():
        log("    ✗ Not admin — restore needs elevation.")
        return False
    try:
        with open(HOSTS_BACKUP, "r", encoding="utf-8",
                  errors="surrogateescape") as src, \
             open(HOSTS_PATH, "w", encoding="utf-8",
                  errors="surrogateescape") as dst:
            dst.write(src.read())
        log("    ✓ Restored hosts from backup.")
        return True
    except Exception as e:
        log(f"    ✗ Restore failed: {e}")
        return False

def flush_dns():
    rc, _, err = run(["ipconfig", "/flushdns"], timeout=10)
    return rc == 0

# ── Tracert + bottleneck ───────────────────────────────────────────────────
def tracert(host, max_hops=15):
    rc, out, err = run(["tracert", "-d", "-h", str(max_hops),
                        "-w", "2000", host], timeout=60)
    if rc != 0:
        return None
    return parse_tracert_output(out)

def parse_tracert_output(out):
    """Parse `tracert -d` text into [{"hop":n, "ip":str, "avg_ms":int|None}]."""
    hops = []
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"(\d+)\s+(.+)", line)
        if not m:
            continue
        rest = m.group(2)
        ms_vals = re.findall(r"(<\d+|\d+)\s*ms", rest)
        ip_m = re.search(r"(\d+\.\d+\.\d+\.\d+)", rest)
        nums = [0 if mv.startswith("<") else int(mv) for mv in ms_vals]
        avg = round(sum(nums) / len(nums)) if nums else None
        hops.append({"hop": int(m.group(1)),
                     "ip": ip_m.group(1) if ip_m else "?",
                     "avg_ms": avg})
    return hops

def identify_bottleneck(hops):
    """Hop with the largest latency jump (>15 ms) over its predecessor."""
    best = None
    for i in range(1, len(hops)):
        prev, curr = hops[i-1]["avg_ms"], hops[i]["avg_ms"]
        if prev is None or curr is None:
            continue
        jump = curr - prev
        if jump > 15 and (best is None or jump > best[2]):
            best = (hops[i]["hop"], prev, jump, hops[i]["ip"])
    return best

# ── Stats ──────────────────────────────────────────────────────────────────
def pct(data, p):
    if not data:
        return None
    s = sorted(data)
    return s[min(int(len(s) * p), len(s) - 1)]

def jitter(vals):
    if len(vals) < 2:
        return 0
    recent = vals[-5:]
    diffs = [abs(recent[i+1] - recent[i]) for i in range(len(recent) - 1)]
    return round(sum(diffs) / len(diffs), 1)

# ── Scan one host ──────────────────────────────────────────────────────────
def discover_candidates(host):
    sys_v4 = resolve_system(host)
    sys_v6 = resolve_system_v6(host)
    doh_v4, doh_v6 = doh_resolve(host)
    v4 = list(dict.fromkeys(sys_v4 + list(doh_v4.keys())))
    v6 = list(dict.fromkeys(sys_v6 + list(doh_v6.keys())))
    return {
        "v4": v4,
        "v6": v6,
        "doh_v4_sources": {ip: sorted(list(s)) for ip, s in doh_v4.items()},
        "doh_v6_sources": {ip: sorted(list(s)) for ip, s in doh_v6.items()},
        "sys_v4": sys_v4,
        "sys_v6": sys_v6,
    }

def scan_host(host, port=443, verbose=True):
    """Full discovery + parallel scan. Returns dict with best edge and all edges."""
    disc = discover_candidates(host)
    all_ips = disc["v4"] + disc["v6"]

    if verbose:
        new_doh = [ip for ip in disc["doh_v4_sources"] if ip not in disc["sys_v4"]]
        log(f"  {host}  ->  {len(disc['v4'])} v4 + {len(disc['v6'])} v6 candidates")
        if new_doh:
            log(f"      DoH-only edges: {', '.join(new_doh[:5])}"
                f"{'...' if len(new_doh) > 5 else ''}")

    if not all_ips:
        return {"host": host, "best": None, "edges": {}, "disc": disc}

    results = scan_edges(all_ips, port=port, workers=16, samples=3)

    if verbose:
        for ip in sorted(results, key=lambda x: results[x]["median"]):
            r = results[ip]
            tag = "v6" if ":" in ip else "v4"
            src = disc["doh_v4_sources"].get(ip) or disc["doh_v6_sources"].get(ip)
            src_s = f"  via {'/'.join(src)}" if src else ""
            log(f"      {ip:42s} {r['median']:4d}ms  jit {r['jitter']:>3}ms  "
                f"[{tag} {r['method']}]{src_s}")

    best_ip = min(results, key=lambda x: results[x]["median"]) if results else None
    best = results.get(best_ip) if best_ip else None

    return {
        "host": host,
        "best": best_ip,
        "best_ms": best["median"] if best else None,
        "best_jitter": best["jitter"] if best else None,
        "best_method": best["method"] if best else None,
        "edges": {ip: results[ip]["median"] for ip in results},
        "edge_jitter": {ip: results[ip]["jitter"] for ip in results},
        "edge_method": {ip: results[ip]["method"] for ip in results},
        "disc": disc,
        "ts": time.time(),
    }

def stability_gate(entry):
    """Decide whether an edge is safe to write into hosts.

    Over the last 3 rounds (fewer if fewer were run) it requires:
      - every round answered — a dead round can never count as agreement
      - the same best IP in >= 2 rounds (>= 1 when only 1 round was run,
        so --rounds 1 is eligible but still bound by SLA + jitter below)
      - latest round: median < SLA and jitter <= STABILITY_MAX_JITTER
    Returns (ok, reason).
    """
    hist = entry.get("history", [])
    if not hist:
        return False, "no history"
    last = hist[-min(3, len(hist)):]
    if any(h.get("best") is None for h in last):
        return False, "a scan round found no reachable edge"
    best_counts = {}
    for h in last:
        best_counts[h["best"]] = best_counts.get(h["best"], 0) + 1
    winner = max(best_counts, key=best_counts.get)
    votes = best_counts[winner]
    need = 2 if len(last) >= 2 else 1
    if votes < need:
        return False, f"edge flapped across rounds (only {votes}/{len(last)} agreed)"
    final = last[-1]
    if final["best"] != winner:
        return False, f"latest best {final['best']} differs from stable winner {winner}"
    if final["ms"] is None or final["ms"] >= SLA_MS:
        return False, f"median {final['ms']}ms >= SLA {SLA_MS}ms"
    if final["jitter"] is None or final["jitter"] > STABILITY_MAX_JITTER:
        return False, f"jitter {final['jitter']}ms > {STABILITY_MAX_JITTER}ms"
    return True, f"stable winner {winner} ({final['ms']}ms, jitter {final['jitter']}ms)"

# ── Session runner ─────────────────────────────────────────────────────────
@dataclass
class SessionConfig:
    """Everything run_session needs. `stop_event` is a threading.Event (or
    None): set it to stop a running session cleanly — the final report is
    still written. The CLI passes None and lets KeyboardInterrupt do this."""
    targets: list
    minutes: int = SESSION_MINUTES_DEFAULT
    interval: int = PING_INTERVAL_DEFAULT
    aggressive: bool = False
    apply_hosts: bool = False
    rounds: int = STABILITY_ROUNDS
    mode: str = "full"              # "full" | "scan_only"
    stop_event: object = None

def _stopped(stop_event):
    return stop_event is not None and stop_event.is_set()

def _sleep(stop_event, seconds):
    """time.sleep() that returns early once stop_event is set.
    With no stop_event this is an ordinary uninterrupted sleep (CLI path)."""
    if stop_event is None:
        time.sleep(seconds)
        return
    end = time.time() + seconds
    while time.time() < end:
        if stop_event.is_set():
            return
        time.sleep(min(0.2, max(0.0, end - time.time())))

def _host_scan_data(host, entry, round_no):
    """Structured payload for a host_scan event (JSON-safe)."""
    disc = entry.get("disc") or {}
    return {
        "host": host,
        "round": round_no,
        "best": entry.get("best"),
        "best_ms": entry.get("best_ms"),
        "best_jitter": entry.get("best_jitter"),
        "best_method": entry.get("best_method"),
        "edges": entry.get("edges", {}),
        "edge_jitter": entry.get("edge_jitter", {}),
        "edge_method": entry.get("edge_method", {}),
        "doh_v4_sources": disc.get("doh_v4_sources", {}),
        "doh_v6_sources": disc.get("doh_v6_sources", {}),
        "disc": disc,
        "ts": entry.get("ts"),
    }

def _scan_rounds(targets, rounds, emit, verbose=True, stop_event=None):
    """Run N scan rounds per host. Return final per-host entry."""
    results = {}
    for r in range(1, rounds + 1):
        if rounds > 1:
            emit("round", {"n": r, "total": rounds}, f"\n  > Round {r}/{rounds}")
        for host, port, *_ in targets:
            if _stopped(stop_event):
                return results
            entry = scan_host(host, port=port, verbose=verbose)
            prev = results.get(host, {})
            hist = prev.get("history", [])
            # Record every round — including rounds where nothing answered —
            # so the stability gate can never treat a dead round as agreement.
            hist.append({"best": entry.get("best"),
                         "ms": entry.get("best_ms"),
                         "jitter": entry.get("best_jitter"),
                         "ts": time.time()})
            entry["history"] = hist
            results[host] = entry
            emit("host_scan", _host_scan_data(host, entry, r), None)
        if _stopped(stop_event):
            return results
        if r < rounds:
            _sleep(stop_event, 2)
    return results

def _rescan_priority(scan_results, emit):
    """Re-evaluate the priority host's best edge while monitoring.

    Report-only: never re-pins and never touches the hosts file — it just
    warns when a different or faster edge appears so you can re-run with
    --apply-hosts if you want to lock it.
    """
    entry = scan_host(PRIORITY_HOST, port=443, verbose=False)
    if not entry.get("best"):
        log(f"  ! re-scan: {PRIORITY_HOST} has no reachable edge right now")
        emit("rescan", {"host": PRIORITY_HOST,
                        "prev_best": (scan_results.get(PRIORITY_HOST) or {}).get("best"),
                        "cur_best": None, "cur_ms": None, "reachable": False}, None)
        return
    prev = scan_results.get(PRIORITY_HOST, {})
    prev_best, prev_ms = prev.get("best"), prev.get("best_ms")
    cur_ms = entry["best_ms"]
    if prev_best and entry["best"] != prev_best:
        log(f"  ! re-scan: {entry['best']} is now fastest at {cur_ms}ms — "
            f"pinned {prev_best} at {prev_ms}ms. Re-run with --apply-hosts "
            f"to lock the new edge.")
    else:
        log(f"  . re-scan: {PRIORITY_HOST} still best on {entry['best']} "
            f"({cur_ms}ms)")
    emit("rescan", {"host": PRIORITY_HOST, "prev_best": prev_best,
                    "prev_ms": prev_ms, "cur_best": entry["best"],
                    "cur_ms": cur_ms, "reachable": True}, None)

def run_session(cfg, emit=None):
    """Run a scan/monitor session and return a result dict.

    emit(kind, data, msg) receives structured events — kinds: round,
    host_scan, gate, sla_verdict, bottleneck, hosts_write, sample, rescan,
    report, done. When msg is not None it IS the console line for that event
    and is NOT also sent through the log sink; frontends append event
    messages and sink lines to their log view exactly once each.

    Raises SessionAborted for unrecoverable setup failures (exit code 1 for
    the CLI). KeyboardInterrupt (CLI) or cfg.stop_event (GUI) stops the
    monitor loop early — the final report is still written.
    """
    if emit is None:
        emit = _emit_null
    if cfg.mode == "scan_only":
        return _session_scan_only(cfg, emit)
    if cfg.mode == "full":
        return _session_full(cfg, emit)
    raise ValueError(f"unknown session mode: {cfg.mode!r}")

def _session_scan_only(cfg, emit):
    log("── Scan-only mode ──")
    results = _scan_rounds(cfg.targets, cfg.rounds, emit,
                           stop_event=cfg.stop_event, verbose=True)
    log("── Done. No system changes were made. ──")
    stopped = _stopped(cfg.stop_event)
    emit("done", {"hosts_written": False, "stopped": stopped}, None)
    return {"scan_results": results, "stats": {}, "monitor_hosts": [],
            "hosts_written": False, "stopped": stopped, "report": None}

def _session_full(cfg, emit):
    targets = cfg.targets
    minutes = cfg.minutes
    interval = cfg.interval
    aggressive = cfg.aggressive
    apply_hosts = cfg.apply_hosts
    rounds = cfg.rounds
    stop_event = cfg.stop_event

    log("╔══════════════════════════════════════════════════════════╗")
    log("║   Roblox setup.roblox.com — Latency Optimizer (v7.1)     ║")
    log("║   Focus: setup.roblox.com under 50 ms                    ║")
    log("╚══════════════════════════════════════════════════════════╝")
    log(f"  Session : {minutes} min | Interval: {interval}s")
    log(f"  Admin   : {is_admin()}")
    log(f"  SLA     : {SLA_MS} ms (priority: {PRIORITY_HOST})")
    log(f"  Hosts   : {'WILL BE WRITTEN' if apply_hosts else 'read-only (use --apply-hosts to write)'}")
    log(f"  Log dir : {LOG_DIR}")

    if not ensure_log_dir():
        log("✗ Cannot write log files — aborting before any measurement.")
        raise SessionAborted("cannot create log dir")

    load_cache()

    log("\n── Phase 1: Edge discovery + multi-round stability ──")
    log(f"  Running {rounds} scan round(s) per host. Edge must agree across")
    log(f"  rounds, stay < {SLA_MS} ms, and have jitter <= {STABILITY_MAX_JITTER} ms")
    log("  before it's eligible for hosts-file lock.\n")

    scan_results = _scan_rounds(targets, rounds, emit,
                                stop_event=stop_event, verbose=True)

    # Show summary of best per host
    log("\n── Scan summary ──")
    hosts_eligible = {}
    for host, entry in scan_results.items():
        if not entry.get("best"):
            emit("gate", {"host": host, "ok": False,
                          "reason": "no reachable edges", "entry": entry},
                 f"  {host:42s}  no reachable edges")
            continue
        ok, reason = stability_gate(entry)
        mark = "✓" if ok else "✗"
        emit("gate", {"host": host, "ok": ok, "reason": reason,
                      "entry": entry, "best": entry["best"],
                      "best_ms": entry["best_ms"],
                      "best_jitter": entry["best_jitter"]},
             f"  {mark} {host:42s}  best {entry['best']:18s} "
             f"{entry['best_ms']:3d}ms  jit {entry['best_jitter']}ms  — {reason}")
        if ok:
            hosts_eligible[host] = entry
        _cache[host] = {
            "best": entry["best"],
            "best_ms": entry["best_ms"],
            "edges": entry["edges"],
            "history": entry.get("history", []),
            "ts": time.time(),
        }
    save_cache()

    # ── SLA verdict for setup.roblox.com ──
    pri = scan_results.get(PRIORITY_HOST)
    if pri and pri.get("best_ms") is not None:
        log("")
        if pri["best_ms"] < SLA_MS:
            emit("sla_verdict", {"host": PRIORITY_HOST, "ms": pri["best_ms"],
                                 "under": True},
                 f"  ✓ {PRIORITY_HOST} best median {pri['best_ms']}ms — UNDER {SLA_MS}ms SLA")
        else:
            emit("sla_verdict", {"host": PRIORITY_HOST, "ms": pri["best_ms"],
                                 "under": False},
                 f"  ⚠ {PRIORITY_HOST} best median {pri['best_ms']}ms — OVER {SLA_MS}ms SLA")
            log("    The closest reachable edge is that far away. If the")
            log("    bottleneck is ISP-side, no local tuning can fix it.")
            log("    Running tracert to find the bottleneck hop...")
            hops = tracert(PRIORITY_HOST, max_hops=15)
            bn = identify_bottleneck(hops) if hops else None
            emit("bottleneck", {"host": PRIORITY_HOST, "hops": hops,
                                "bottleneck": bn}, None)
            if hops:
                if bn:
                    log(f"    🔍 Bottleneck: hop {bn[0]} ({bn[3]}) +{bn[2]}ms")
                    log("       Local tuning can't fix this — consider a gaming")
                    log("       VPN or an ISP with better routing to the CDN edge.")
                else:
                    log("    No single-hop bottleneck found — latency accumulates gradually.")
    elif any(h == PRIORITY_HOST for h, *_ in targets):
        emit("sla_verdict", {"host": PRIORITY_HOST, "ms": None, "under": False},
             f"  ⚠ {PRIORITY_HOST} did not respond to either ICMP or TCP 443.")
        log("    Check firewall / try a VPN / verify the host resolves.")

    # ── Optionally write hosts ──
    hosts_written = False
    if apply_hosts:
        log("\n── Phase 2: Hosts-file lock ──")
        if not is_admin():
            log("  ✗ Admin required to modify hosts. Skipping.")
            emit("hosts_write", {"ok": False, "mapping": {},
                                 "error": "admin required"}, None)
        elif not hosts_eligible:
            log("  ✗ No host passed the stability gate. Hosts NOT modified.")
            emit("hosts_write", {"ok": False, "mapping": {},
                                 "error": "no eligible hosts"}, None)
        else:
            mapping = {host: entry["best"]
                       for host, entry in hosts_eligible.items()}
            for host, entry in hosts_eligible.items():
                log(f"  → {host:42s} → {entry['best']}  ({entry['best_ms']}ms)")
            log(f"\n  Writing {len(mapping)} entries to hosts...")
            if write_hosts(mapping):
                hosts_written = True
                flush_dns()
                log("  ✓ hosts updated. Run --restore to revert, and re-run")
                log("    in 1-2 weeks (CDN IPs rotate — stale entries can break")
                log("    Roblox).")
                emit("hosts_write", {"ok": True, "mapping": mapping,
                                     "error": None}, None)
            else:
                log("  ✗ hosts write failed.")
                emit("hosts_write", {"ok": False, "mapping": mapping,
                                     "error": "write failed"}, None)

    return _session_monitor(cfg, emit, scan_results, hosts_written)

def _session_monitor(cfg, emit, scan_results, hosts_written):
    """Phase 3: live monitoring loop (stop_event/KeyboardInterrupt end it
    early — the report is always written afterwards)."""
    minutes = cfg.minutes
    interval = cfg.interval
    stop_event = cfg.stop_event

    rescan_every = RESCAN_AGGRESSIVE_SEC if cfg.aggressive else RESCAN_DEFAULT_SEC
    log(f"\n── Phase 3: Live monitoring ({minutes} min, every {interval}s) ──")
    log("  Watching targets against the pinned edges.")
    if PRIORITY_HOST in scan_results:
        log(f"  Re-scanning {PRIORITY_HOST} every {rescan_every}s for better "
            f"edges{' (aggressive)' if cfg.aggressive else ''}.")
    log("  Press Ctrl+C to stop early.\n")

    monitor_hosts = []
    for h, p, *rest in cfg.targets:
        if h in scan_results and scan_results[h].get("best"):
            monitor_hosts.append((h, p, rest[0] if rest else h))

    if not monitor_hosts:
        log("  ✗ No hosts to monitor. Exiting.")
        emit("done", {"hosts_written": hosts_written, "stopped": False}, None)
        return {"scan_results": scan_results, "stats": {},
                "monitor_hosts": [], "hosts_written": hosts_written,
                "stopped": False, "report": None}

    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["timestamp", "host", "pinned_ip", "latency_ms", "method",
                 "below_sla", "note"])

    stats = {h: {"vals": [], "timeouts": 0, "sla_breach": 0,
                 "pinned": scan_results[h]["best"]}
             for h, _, _ in monitor_hosts}
    end_time = time.time() + minutes * 60
    last_rescan = time.time()
    stopped = False

    try:
        while time.time() < end_time:
            if _stopped(stop_event):
                stopped = True
                log("\n⏹  Stopped by user.")
                break
            for host, port, label in monitor_hosts:
                pinned = stats[host]["pinned"]
                ms, method = measure(pinned, port=port)

                if ms is None:
                    stats[host]["timeouts"] += 1
                    emit("sample", {"host": host, "pinned_ip": pinned,
                                    "ms": None, "method": "N/A",
                                    "below_sla": None, "timeout": True,
                                    "ts": time.time()},
                         f"  {host:42s}  TIMEOUT on pinned {pinned}")
                    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
                        csv.writer(f).writerow([
                            datetime.datetime.now().isoformat(),
                            host, pinned, "TIMEOUT", "N/A", "", "timeout"])
                    continue

                stats[host]["vals"].append(ms)
                below = ms < SLA_MS
                if not below and host == PRIORITY_HOST:
                    stats[host]["sla_breach"] += 1
                    emit("sample", {"host": host, "pinned_ip": pinned,
                                    "ms": ms, "method": method,
                                    "below_sla": False, "timeout": False,
                                    "ts": time.time()},
                         f"  ⚠ {host:42s}  {ms:4d}ms  ({method})  "
                         f"OVER {SLA_MS}ms SLA  [pinned {pinned}]")
                else:
                    tag = "✓" if below else " "
                    emit("sample", {"host": host, "pinned_ip": pinned,
                                    "ms": ms, "method": method,
                                    "below_sla": below, "timeout": False,
                                    "ts": time.time()},
                         f"  {tag} {host:42s}  {ms:4d}ms  ({method})  "
                         f"[pinned {pinned}]")

                with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow([
                        datetime.datetime.now().isoformat(),
                        host, pinned, ms, method, 1 if below else 0, ""])

            if (PRIORITY_HOST in scan_results
                    and time.time() - last_rescan >= rescan_every):
                last_rescan = time.time()
                _rescan_priority(scan_results, emit)

            _sleep(stop_event, interval)

    except KeyboardInterrupt:
        stopped = True
        log("\n⏹  Stopped by user.")

    return _session_report(cfg, emit, scan_results, stats, monitor_hosts,
                           hosts_written, stopped)

def _session_report(cfg, emit, scan_results, stats, monitor_hosts,
                    hosts_written, stopped):
    """Phase 4: summary text + JSON report, always written, even when stopped."""
    minutes = cfg.minutes
    interval = cfg.interval

    log("\n── Phase 4: Report ──")
    lock = ("YES" if hosts_written
            else ("attempted (not eligible or failed)" if cfg.apply_hosts else "no"))
    lines = [
        "=" * 62,
        "  Roblox setup.roblox.com — Latency Report",
        "=" * 62,
        "",
        f"  Session    : {minutes} min @ {interval}s",
        f"  SLA target : {PRIORITY_HOST} < {SLA_MS} ms",
        f"  Admin      : {is_admin()}",
        f"  Hosts lock : {lock}",
        "",
    ]

    if PRIORITY_HOST in stats:
        s = stats[PRIORITY_HOST]
        if s["vals"]:
            vals = s["vals"]
            below = sum(1 for v in vals if v < SLA_MS)
            p50 = pct(vals, 0.5)
            p95 = pct(vals, 0.95)
            pct_below = below * 100 // len(vals)
            verdict = ("✓ PASS" if pct_below >= 90
                       else "≈ PARTIAL" if pct_below >= 60 else "⚠ FAIL")
            lines += [
                f"── {PRIORITY_HOST} ──",
                f"  {verdict}: {pct_below}% of samples under {SLA_MS}ms",
                f"  min / p50 / p95 / max : "
                f"{min(vals)} / {p50} / {p95} / {max(vals)} ms",
                f"  jitter (recent 5)     : {jitter(vals)} ms",
                f"  samples               : {len(vals)}  "
                f"(timeouts: {s['timeouts']}, SLA breaches: {s['sla_breach']})",
                "",
            ]
            if pct_below < 90:
                lines += [
                    f"  Note: {PRIORITY_HOST} could not stay under {SLA_MS}ms",
                    f"        for {100 - pct_below}% of samples. This is ISP-side",
                    "        routing or the CDN edge is physically far.",
                    "",
                ]

    lines.append("── All targets ──")
    for host, _, _ in monitor_hosts:
        s = stats[host]
        vals = s["vals"]
        if not vals:
            lines.append(f"  {host:42s}  no successful samples")
            continue
        lines.append(
            f"  {host:42s}  min {min(vals):3d}  "
            f"p50 {pct(vals, 0.5):3.0f}  p95 {pct(vals, 0.95):3.0f}  "
            f"jit {jitter(vals):4.1f}  (pinned {s['pinned']})")
    lines.append("")

    lines.append("── Edges discovered (from scan) ──")
    for host, entry in scan_results.items():
        if not entry.get("edges"):
            continue
        lines.append(f"  {host}:")
        top = sorted(entry["edges"].items(), key=lambda x: x[1])[:5]
        for ip, ms in top:
            j = entry["edge_jitter"].get(ip, "?")
            m = entry["edge_method"].get(ip, "?")
            lines.append(f"    {ip:42s}  {ms:4d}ms  jitter {j}ms  [{m}]")
        lines.append("")

    lines += [
        f"  Log     : {LOG_FILE}",
        f"  Summary : {SUMMARY_FILE}",
        f"  Report  : {REPORT_FILE}",
        f"  Cache   : {CACHE_FILE}",
        f"  Hosts   : {HOSTS_PATH}  (backup: {HOSTS_BACKUP})",
        "",
    ]

    text = "\n".join(lines)
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    for line in lines:
        log(line)

    # JSON report
    report = {
        "session_minutes": minutes,
        "interval_sec": interval,
        "sla_ms": SLA_MS,
        "admin": is_admin(),
        "hosts_written": hosts_written,
        "aggressive": bool(cfg.aggressive),
        "rounds": cfg.rounds,
        "scan": {h: {
            "best": e.get("best"),
            "best_ms": e.get("best_ms"),
            "edges": e.get("edges", {}),
            "edge_jitter": e.get("edge_jitter", {}),
            "edge_method": e.get("edge_method", {}),
            "doh_v4_sources": e.get("disc", {}).get("doh_v4_sources", {}),
            "sys_v4": e.get("disc", {}).get("sys_v4", []),
        } for h, e in scan_results.items()},
        "monitor": {h: {
            "pinned": stats[h]["pinned"],
            "samples": stats[h]["vals"],
            "count": len(stats[h]["vals"]),
            "min": min(stats[h]["vals"]) if stats[h]["vals"] else None,
            "p50": pct(stats[h]["vals"], 0.5),
            "p95": pct(stats[h]["vals"], 0.95),
            "max": max(stats[h]["vals"]) if stats[h]["vals"] else None,
            "timeouts": stats[h]["timeouts"],
            "sla_breaches": stats[h]["sla_breach"],
        } for h in stats},
        "priority_sla_pass_pct": (
            sum(1 for v in stats[PRIORITY_HOST]["vals"] if v < SLA_MS)
            * 100 // max(1, len(stats[PRIORITY_HOST]["vals"]))
            if PRIORITY_HOST in stats and stats[PRIORITY_HOST]["vals"] else None
        ),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    emit("report", {"summary_text": text, "report_json": report,
                    "paths": {"log": LOG_FILE, "summary": SUMMARY_FILE,
                              "report": REPORT_FILE, "cache": CACHE_FILE,
                              "hosts": HOSTS_PATH,
                              "hosts_backup": HOSTS_BACKUP}}, None)

    log("\n✓ Done.")
    log(f"  summary : {SUMMARY_FILE}")
    log(f"  report  : {REPORT_FILE}")
    log(f"  csv     : {LOG_FILE}")
    if hosts_written:
        log("\n⚠ REMINDER: the hosts file was modified. Run with --restore to")
        log("  revert when you're done testing, or if Roblox misbehaves later.")

    emit("done", {"hosts_written": hosts_written, "stopped": stopped}, None)
    return {"scan_results": scan_results, "stats": stats,
            "monitor_hosts": monitor_hosts,
            "hosts_written": hosts_written, "stopped": stopped,
            "report": report}

# ── Elevated-apply spec ─────────────────────────────────────────────────────
def parse_apply_spec(spec):
    """Parse 'host=ip,host2=ip2' into {host: ip} for the elevated-apply path.

    Both sides are validated here AND again by the elevated child, so a
    crafted string can never reach the hosts file or a subprocess.
    Raises ValueError on anything questionable.
    """
    if not spec or not spec.strip():
        raise ValueError("empty apply spec")
    mapping = {}
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        host, sep, ip = raw.partition("=")
        if not sep:
            raise ValueError(f"invalid apply entry (need host=ip): {raw!r}")
        host = host.strip().rstrip(".")
        ip = ip.strip()
        if not valid_host(host):
            raise ValueError(
                f"invalid host in apply spec: {raw!r} "
                f"(letters, digits, dots and hyphens only)")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError(
                f"apply spec host must be a name, not an IP: {raw!r}")
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            raise ValueError(f"invalid IP in apply spec: {raw!r}") from None
        mapping[host] = ip
    if not mapping:
        raise ValueError("apply spec did not contain any host=ip entries")
    return mapping
