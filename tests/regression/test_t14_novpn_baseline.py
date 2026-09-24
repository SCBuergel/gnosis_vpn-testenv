"""T14-novpn-baseline (diagnostic): is the target itself healthy and fast? The same REPS transfers of BYTES from
the client container straight to the target's direct address, tunnel disconnected, plus the direct RTT. Once per
run. PASS iff every download and upload completes; as a diagnostic a miss is WARN; rates are recorded.

Why: it is the control that makes a slow cell attributable. The fleet's target delivered ~198 Mbit/s at 1.9 ms,
so every tunnelled figure below that belonged to the VPN; without it 'the target got slow' stays a live
explanation for every regression."""
from suitelib.target import ping_avg, summary_row, transfer_series

TEST = "T14-novpn-baseline"
KIND = "diagnostic"
GROUP = "throughput"
KNOBS = {}


def test_novpn_baseline(cfg, client, target, checks, knobs):
    if client.is_connected():
        client.disconnect()
    client.iface = "eth0"
    summ = transfer_series(checks, client, "baseline", target.ip_direct, cfg.reps, cfg.bytes, cfg.cap)
    rtt = ping_avg(client, target.ip_direct)
    checks.row(kind="summary", summary=summary_row(summ), rtt_ms=rtt)
    dc, uc = summ["down_complete"], summ["up_complete"]
    msg = f"down {summ['down_median']} up {summ['up_median']} Mbit/s, complete {dc}/{cfg.reps} + {uc}/{cfg.reps}, rtt {rtt} ms"
    checks.verdict(dc == cfg.reps and uc == cfg.reps, msg)
