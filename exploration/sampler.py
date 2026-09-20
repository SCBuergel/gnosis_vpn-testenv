#!/usr/bin/env python3
"""Host-side sampler for the disconnect deep dive. Every INTERVAL seconds records, with a wall-clock stamp:
  - the client tunnel interface rx/tx byte counters (download / upload progress),
  - the client's active-session SURB buffer estimate (max hopr_session_surb_buffer_estimate over sessions),
  - the exit node's SURB balancer buffer estimate / target / rate (max over sessions).
Writes CSV to --out until killed. Every probe is wrapped so one failure never stops sampling."""
import argparse, subprocess, time, sys

ap = argparse.ArgumentParser()
ap.add_argument("--client", default="gnosis_vpn-client")
ap.add_argument("--wg", required=True)
ap.add_argument("--node-url", required=True)
ap.add_argument("--node-token", default="")
ap.add_argument("--interval", type=float, default=1.0)
ap.add_argument("--out", required=True)
a = ap.parse_args()

def sh(cmd, timeout=4):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""

def wg_bytes():
    out = sh(["docker", "exec", a.client, "sh", "-c",
              f"cat /sys/class/net/{a.wg}/statistics/rx_bytes /sys/class/net/{a.wg}/statistics/tx_bytes 2>/dev/null"])
    v = out.split()
    return (v[0], v[1]) if len(v) == 2 else ("", "")

def client_surb():
    out = sh(["docker", "exec", a.client, "gnosis_vpn-ctl", "telemetry"], timeout=6)
    m = 0
    for line in out.splitlines():
        line = line.strip().strip('"')
        if line.startswith("hopr_session_surb_buffer_estimate{"):
            try: m = max(m, float(line.rsplit(" ", 1)[1]))
            except Exception: pass
    return int(m)

def exit_surb():
    out = sh(["curl", "-s", "-m", "4", "-H", "x-auth-token: " + a.node_token, a.node_url + "/metrics"])
    est = tgt = rate = None
    for line in out.splitlines():
        if line.startswith("#"): continue
        try:
            if line.startswith("hopr_surb_balancer_current_buffer_estimate"):
                v = float(line.rsplit(" ", 1)[1]); est = max(est or 0, v)
            elif line.startswith("hopr_surb_balancer_current_buffer_target"):
                v = float(line.rsplit(" ", 1)[1]); tgt = max(tgt or 0, v)
            elif line.startswith("hopr_surb_balancer_surbs_rate"):
                v = float(line.rsplit(" ", 1)[1]); rate = v if rate is None else rate + v
        except Exception:
            pass
    return (est, tgt, rate)

with open(a.out, "w", buffering=1) as f:
    f.write("ts,wg_rx,wg_tx,client_surb,exit_buf_est,exit_buf_target,exit_surb_rate\n")
    while True:
        ts = time.time()
        rx, tx = wg_bytes()
        cs = client_surb()
        est, tgt, rate = exit_surb()
        f.write(f"{ts:.3f},{rx},{tx},{cs},{est if est is not None else ''},{tgt if tgt is not None else ''},{rate if rate is not None else ''}\n")
        dt = a.interval - (time.time() - ts)
        if dt > 0: time.sleep(dt)
