#!/usr/bin/env python3
"""Merge a deep-dive arm's sampler CSV with its client log into one timeline, so the SURB buffer and throughput
can be read off against the exact ping-timeout / reconnect moments.

Usage: analyze-deepdive.py <run_dir> <arm_name>
Reads <run_dir>/sample-<arm>.csv and <run_dir>/logs/<arm>.log, prints a per-second table and the reconnect
chain with the SURB buffer at each ping timeout."""
import sys, re, csv, datetime

run, arm = sys.argv[1], sys.argv[2]
csvp = f"{run}/sample-{arm}.csv"
logp = f"{run}/logs/{arm}.log"

# --- sampler rows -> per-interval throughput (Mbit/s) and SURB levels ---
rows = []
try:
    with open(csvp) as f:
        for r in csv.DictReader(f):
            rows.append(r)
except FileNotFoundError:
    print(f"[{arm}] no sampler csv"); sys.exit(0)

def num(x):
    try: return float(x)
    except Exception: return None

series = []
prev = None
for r in rows:
    ts = num(r["ts"]); rx = num(r["wg_rx"]); tx = num(r["wg_tx"])
    dl = ul = None
    if prev and ts and prev[0]:
        dt = ts - prev[0]
        if dt > 0:
            if rx is not None and prev[1] is not None: dl = (rx - prev[1]) * 8 / dt / 1e6
            if tx is not None and prev[2] is not None: ul = (tx - prev[2]) * 8 / dt / 1e6
    series.append((ts, dl, ul, num(r["client_surb"]), num(r["exit_buf_est"]), num(r["exit_buf_target"])))
    prev = (ts, rx, tx)

t0 = series[0][0] if series else 0

# --- log: ping timeouts, ping oks, reconnect, link removed, tun created (with epoch seconds) ---
ansi = re.compile(r"\x1b\[[0-9;]*m")
tsre = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)")
def epoch(line):
    m = tsre.search(line)
    if not m: return None
    s = m.group(1).replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try: return datetime.datetime.strptime(s, fmt).replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError: pass
    return None

events = []
try:
    for line in open(logp, errors="ignore"):
        line = ansi.sub("", line)
        for pat, tag in [
            (r"TunnelPingResult: Error\(Ping timed out\)", "PING_TIMEOUT"),
            (r"tunnel ping .*exceeded max failures - reconnecting", "RECONNECT"),
            (r"network link removed", "LINK_REMOVED"),
            (r"created TUN device", "TUN_CREATED"),
            (r"worker process exited", "WORKER_RESTART"),
        ]:
            if re.search(pat, line):
                e = epoch(line)
                if e: events.append((e, tag))
                break
except FileNotFoundError:
    pass

def rel(t): return f"{t-t0:6.1f}" if (t and t0) else "   n/a"

print(f"\n[{arm}] timeline (t=0 at first sample; dl/ul Mbit/s; client_surb; exit buf est/target)")
print(f"{'t+s':>6} {'dl':>7} {'ul':>7} {'csurb':>7} {'ebuf':>7} {'etgt':>7}  events")
ev_by_sec = {}
for e, tag in events:
    ev_by_sec.setdefault(round(e), []).append(tag)
for ts, dl, ul, cs, eb, et in series:
    if ts is None: continue
    marks = ",".join(ev_by_sec.get(round(ts), []))
    print(f"{rel(ts)} {('' if dl is None else f'{dl:7.2f}')} {('' if ul is None else f'{ul:7.2f}')} "
          f"{('' if cs is None else f'{int(cs):7d}')} {('' if eb is None else f'{int(eb):7d}')} "
          f"{('' if et is None else f'{int(et):7d}')}  {marks}")

# SURB level at each ping timeout
print(f"\n[{arm}] SURB buffer at each ping event:")
for e, tag in events:
    # nearest sample at or before e
    best = None
    for ts, dl, ul, cs, eb, et in series:
        if ts and ts <= e + 0.5: best = (ts, cs, eb, et)
    if best:
        print(f"  t+{e-t0:6.1f}  {tag:14s} client_surb={best[1]} exit_buf_est={best[2]} exit_buf_target={best[3]}")
    else:
        print(f"  t+{e-t0:6.1f}  {tag}")
if not events:
    print(f"  [{arm}] no ping-timeout / reconnect events in the log")
