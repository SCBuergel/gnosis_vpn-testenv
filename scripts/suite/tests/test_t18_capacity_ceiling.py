"""T18-capacity-ceiling (diagnostic): download-only UDP rate ladder until delivery collapses, node CPU sampled.
Reports the knee (last rate with loss < 5 %) and CPU per node; below the knee the tunnel watchdog must not fire,
above it the reconnects are counted (the former watchdog-under-saturation test is the top rung)."""
import statistics as st
import time

from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import num

TEST = "T18-capacity-ceiling"
KIND = "diagnostic"
KNOBS = dict(LADDER=q("1 2 4 8 12 16", "2 4 8 12"), STEP_S=q(60, 30))


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
