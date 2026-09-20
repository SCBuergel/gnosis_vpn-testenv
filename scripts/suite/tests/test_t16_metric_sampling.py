"""T16-metric-sampling (diagnostic): 1 Hz samples of every node's counters and CPU, the client and server
container CPU, plus the client's balancer estimate, during one download+upload; then summary numbers: exit-side
session target and estimate (max over sessions, not the sum: closed sessions stay in the gauge for hours),
relay egress ring drops, rejected counters, and the balancer-level vs per-second-throughput correlation (report
the slope, not the gauge). Covariate: the health-check sessions the exit served during the window."""
import json
import statistics as st
import time

from suitelib.client import connect_or_fail
from suitelib.target import curl_down, curl_up

TEST = "T16-metric-sampling"
KIND = "diagnostic"
KNOBS = {}


def analyse(rows, client_csv):
    def series(i, key, agg=sum):
        out = []
        for r in rows:
            n = r["nodes"][i] if i < len(r["nodes"]) else {}
            v = [val for kk, val in n.items() if kk.startswith(key)]
            out.append(agg(v) if v else None)
        return [x for x in out if x is not None]

    res = {}
    for i in range(len(rows[0]["nodes"]) if rows else 0):
        drop = series(i, "hopr_egress_ring_buffer_dropped")
        rej = series(i, "hopr_packet_rejected_count")
        fwd = series(i, 'hopr_packets_count{type="forwarded"}')
        sent = series(i, 'hopr_packets_count{type="sent"}')
        cpu = [r["cpu_pct"].get(f"node{i}") for r in rows if r["cpu_pct"].get(f"node{i}") is not None]
        res[f"node{i}"] = {"egress_drop_delta": (drop[-1] - drop[0]) if drop else None,
                           "rejected_delta": (rej[-1] - rej[0]) if rej else None,
                           "forwarded_pps_max": max((b - a) for a, b in zip(fwd, fwd[1:])) if len(fwd) > 1 else None,
                           "sent_pps_max": max((b - a) for a, b in zip(sent, sent[1:])) if len(sent) > 1 else None,
                           "cpu_pct_mean": round(st.mean(cpu), 1) if cpu else None, "cpu_pct_max": max(cpu) if cpu else None}
    tgt = series(0, "hopr_surb_balancer_current_buffer_target", max)
    est = series(0, "hopr_surb_balancer_current_buffer_estimate", max)
    res["exit_session_target_max"] = max(tgt) if tgt else None
    res["exit_session_estimate_max"] = max(est) if est else None
    ccpu = {}
    for r in rows:
        for kk, v in r.get("container_cpu_pct", {}).items():
            ccpu.setdefault(kk, []).append(v)
    res["container_cpu_mean"] = {kk: round(st.mean(v), 1) for kk, v in ccpu.items()}
    try:
        cl = [l.strip().split(",") for l in open(client_csv) if l.strip()]
        pts = []
        for a, b in zip(cl, cl[1:]):
            try:
                if len(a) >= 3 and a[2] and b[2] and a[1] and b[1]:
                    pts.append((float(a[2]), (int(b[1]) - int(a[1])) * 8 / 1e6))
            except ValueError:
                continue   # the interface vanished for a sample (reconnect): empty fields
        if len(pts) > 5:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            mx, my = st.mean(xs), st.mean(ys)
            sxy = sum((x - mx) * (y - my) for x, y in pts)
            sxx = sum((x - mx) ** 2 for x in xs)
            syy = sum((y - my) ** 2 for y in ys)
            res["balancer_vs_throughput"] = {"n": len(pts), "r": round(sxy / ((sxx * syy) ** 0.5), 3) if sxx > 0 and syy > 0 else None,
                                             "slope_mbit_per_100_surbs": round(100 * sxy / sxx, 4) if sxx > 0 else None}
    except FileNotFoundError:
        pass
    return res


def test_metric_sampling(cfg, run, client, cluster, target, checks, knobs):
    s = connect_or_fail(checks, client, cfg.dest, 5)
    if not s:
        return
    with s:
        with cluster.sampler(run, "t16", 1, [client.name, cfg.server]) as sampler:
            # client balancer estimate at 1 Hz alongside per-second rx bytes
            client.exec_bg(f"i=0; while [ $i -lt 90 ]; do echo $(date +%s),$(cat /sys/class/net/{s.iface}/statistics/rx_bytes),"
                           f"$(gnosis_vpn-ctl -o json telemetry 2>/dev/null | tr -d '\\\\' | grep -oE 'hopr_surb_balancer_current_buffer_estimate[^ ]* [0-9.]+' "
                           f"| head -1 | awk '{{print $2}}') >> {run.in_client}/t16-client.csv; sleep 1; i=$((i+1)); done")
            time.sleep(10)
            r = curl_down(client, target.ip, cfg.bytes * 2, cfg.cap)
            u = curl_up(client, target.ip, cfg.bytes, cfg.cap)
            time.sleep(3)
        rows = sampler.rows()
    an = analyse(rows, run / "t16-client.csv")
    try:
        hc = sum(1 for l in open(cluster.log_file(0), errors="replace") if "got new session request" in l)
    except OSError:
        hc = 0
    checks.row(kind="summary", download=r, upload=u, analysis=an, exit_session_requests_total=hc, destinations=len(client.destinations()))
    print(json.dumps(an))
    checks.record(f"sampled {len(rows)} s; exit target max {an.get('exit_session_target_max')} estimate max {an.get('exit_session_estimate_max')}; "
                  f"node0 sent pps max {an.get('node0', {}).get('sent_pps_max')}; balancer r {(an.get('balancer_vs_throughput') or {}).get('r')}; "
                  f"container cpu {an.get('container_cpu_mean')}")
