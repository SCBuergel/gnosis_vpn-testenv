"""T18-capacity-ceiling (diagnostic): how many packets per second can one exit and one relay carry, and at what
CPU cost? One session, a download stream of STEP_S per rung of LADDER (Mbit/s) with node CPU sampled. Records the
knee (the last rung with loss < 5 %; 8 Mbit/s on the reference stack), CPU per node, and watchdog reconnects: on
rungs below the knee individually, above it summed (the former watchdog-under-saturation test is the top rung).

Why: the fleet's exit hoprd topped out at ~2000 packets/s in+out (about one core), which is why 6 Mbit/s of 1200 B
packets queued to 5-15 s on every relay set; the relay spent 280-360 % CPU forwarding the same rate. Both numbers
are denominators every other test divides by."""
import statistics as st
import time

from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import num

TEST = "T18-capacity-ceiling"
KIND = "diagnostic"
GROUP = "realtime"
KNOBS = dict(LADDER=q("1 2 4 8 12 16", "2 4 8 12"), STEP_S=q(60, 30))
TIMEOUT = lambda k: len(k.words("LADDER")) * (k.STEP_S + 120) + 600   # seconds; the harness fails the test past this


def test_capacity_ceiling(cfg, run, client, cluster, target, checks, knobs):
    k = knobs
    s = connect_or_fail(checks, client, cfg.dest, 15)
    if not s:
        return
    knee, wd_above = 0, 0
    with s:
        for r in k.words("LADDER"):
            with cluster.sampler(run, f"t18-{r}", 1, [client.name, cfg.server]) as sampler:
                client.probe("streamprobe", f"t18-{r}.json", timeout=k.STEP_S + 90, mode="dl", host=target.ip, port=target.stream_port,
                             rate_mbit=r, duration=k.STEP_S, size=1200, iface=s.iface)
            j = run.read_json(f"t18-{r}.json", {"loss_pct": 100})
            rows = sampler.rows()
            keys = {kk for row in rows for kk in row.get("cpu_pct", {})}
            cpu = {kk: round(st.mean([row["cpu_pct"][kk] for row in rows if kk in row.get("cpu_pct", {})]), 1) for kk in keys}
            loss = num(j.get("loss_pct"), 100)
            e = s.errors()
            rec = e["reconnects"]
            checks.row(rate_mbit=float(r), result=j, node_cpu_pct=cpu, errors=e)
            if loss < 5:
                if rec > 0:
                    checks.record(f"watchdog fired {rec}x at {r} Mbit/s while loss was {loss}% (below the knee)")
                knee = r
            else:
                wd_above += rec
            checks.log(f"rate {r} Mbit/s: loss {loss}% p99 {(j.get('delay_over_min_ms') or {}).get('p99')} ms cpu {cpu}")
            time.sleep(5)
    checks.row(kind="summary", knee_mbit=knee, watchdog_reconnects_above_knee=wd_above)
    checks.record(f"ceiling: last clean rung {knee} Mbit/s of ladder [{k.LADDER}]; watchdog reconnects above the knee: {wd_above} "
                  f"(see rows for per-node CPU)")
