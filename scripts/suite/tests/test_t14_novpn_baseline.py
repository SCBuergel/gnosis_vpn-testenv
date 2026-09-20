"""T14-novpn-baseline (diagnostic): the same transfers from the client container straight to the target, no
tunnel. All REPS downloads and uploads complete (a diagnostic, so a miss is WARN); rate and RTT are recorded."""
from suitelib.target import ping_avg, summary_row, transfer_series

TEST = "T14-novpn-baseline"
KIND = "diagnostic"
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
