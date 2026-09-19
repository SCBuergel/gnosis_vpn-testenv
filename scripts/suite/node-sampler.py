#!/usr/bin/env python3
"""Host-side 1 Hz sampler for the regression suite (throughput, real-time-UDP, metric-sampling, capacity tests): per-node hoprd counters from
/metrics, per-pid CPU% from /proc, and container CPU% from cgroup v2 cpu.stat. One JSON line per
sample to --out. Stops on SIGTERM."""
import argparse, json, os, sys, time, urllib.request, subprocess
ap = argparse.ArgumentParser()
ap.add_argument("--pids", default="")
ap.add_argument("--urls", default="")
ap.add_argument("--containers", default="")
ap.add_argument("--interval", type=float, default=1.0)
ap.add_argument("--out", required=True)
a = ap.parse_args()
pids = [p for p in a.pids.split(",") if p]
urls = [u for u in a.urls.split(",") if u]
containers = [c for c in a.containers.split(",") if c]
KEYS = ("hopr_packets_count", "hopr_mixer_queue_size", "hopr_mixer_averaged_delay", "hopr_session_surb_buffer_estimate",
        "hopr_session_surb_target_buffer", "hopr_packet_rejected_count", "hopr_egress_ring_buffer_dropped",
        "hopr_surb_balancer_current_buffer_estimate", "hopr_surb_balancer_target", "hopr_surb_balancer_surbs_rate")
CLK = os.sysconf("SC_CLK_TCK")
def pid_ticks(pid):
    try:
        f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
        return int(f[11]) + int(f[12])
    except Exception:
        return None
def cgroup_usage(name):
    try:
        cid = subprocess.check_output(["docker", "inspect", "-f", "{{.Id}}", name], text=True).strip()
        for p in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat", f"/sys/fs/cgroup/docker/{cid}/cpu.stat"):
            if os.path.exists(p):
                for line in open(p):
                    if line.startswith("usage_usec"):
                        return int(line.split()[1])
    except Exception:
        pass
    return None
def scrape(url):
    out = {}
    try:
        txt = urllib.request.urlopen(url + "/metrics", timeout=2).read().decode()
        for line in txt.splitlines():
            if line.startswith("#"): continue
            for k in KEYS:
                if line.startswith(k):
                    name, _, val = line.rpartition(" ")
                    out[name] = float(val)
    except Exception:
        pass
    return out
prev_t = {p: pid_ticks(p) for p in pids}; prev_c = {c: cgroup_usage(c) for c in containers}; prev_time = time.time()
with open(a.out, "a") as f:
    while True:
        time.sleep(a.interval)
        now = time.time(); dt = now - prev_time; prev_time = now
        row = {"t": round(now, 3), "nodes": [], "cpu_pct": {}, "container_cpu_pct": {}}
        for i, p in enumerate(pids):
            cur = pid_ticks(p)
            if cur is not None and prev_t.get(p) is not None:
                row["cpu_pct"][f"node{i}"] = round(100.0 * (cur - prev_t[p]) / CLK / dt, 1)
            prev_t[p] = cur
        for c in containers:
            cur = cgroup_usage(c)
            if cur is not None and prev_c.get(c) is not None:
                row["container_cpu_pct"][c] = round(100.0 * (cur - prev_c[c]) / 1e6 / dt, 1)
            prev_c[c] = cur
        for i, u in enumerate(urls):
            row["nodes"].append(scrape(u))
        f.write(json.dumps(row) + "\n"); f.flush()
