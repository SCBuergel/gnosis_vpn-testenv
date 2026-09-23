"""T05-loaded-latency (gate): in-tunnel RTT idle, then during a saturating download, then during a saturating
upload, PHASE_S each (p50 and p95 per phase); then N in PARALLEL concurrent downloads of BYTES on the same session,
aggregate Mbit/s per N from the bytes curl actually received over the rung's wall time. Uploads are not run in
parallel: the scaling question is about the SURB-metered return path that downloads ride; T24 covers the forward
path alone. Runs fifth, right after the throughput reference, because loaded RTT taken after an hour of other
tests measures accumulated host load.

Pass iff loaded p95 <= DOWN_P95_MAX_MS in the download phase and <= UP_P95_MAX_MS in the upload phase; every
parallel flow completes within CAP (a rung with an incomplete flow fails naming the count and bytes); and no rung's
aggregate falls below 0.8 x the previous rung's. Until 2026-09-20 the aggregate was N x BYTES over wall time, so
a capped flow still counted as delivered and three runs read exactly the cap (5.33 Mbit/s at N=6).
Calibration: three full runs measured download p95 359, 537, 1076 ms and upload p95 1124, 1153, 1746 ms; the
fleet's bufferbloat finding was 2-4 s.

Why: on the fleet N = 1/3/6 downloads gave 5.7, 5.6, 3.3 Mbit/s while loaded RTT went 216, 511, 722 ms and a
parallel upload hit 8.9 s: the HOPR session path is the cap and its queues bloat. No other test measures latency
under load."""
import time

from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import p95, stats
from suitelib.target import ping_rtts

TEST = "T05-loaded-latency"
KIND = "gate"
KNOBS = dict(PHASE_S=q(30, 15), DOWN_P95_MAX_MS=1500, UP_P95_MAX_MS=2500, PARALLEL="1 3 6")


def test_loaded_latency(cfg, client, target, checks, knobs):
    k = knobs
    s = connect_or_fail(checks, client, cfg.dest, 15)
    if not s:
        return
    with s:
        ping = lambda: ping_rtts(client, target.ip, k.PHASE_S)   # noqa: E731
        idle = ping()
        client.exec_bg(f"curl -s -o /dev/null -m {k.PHASE_S + 5} {target.down_url(400000000)}")
        time.sleep(2)
        dl = ping()
        time.sleep(5)
        client.exec(f"head -c 200000000 /dev/zero > /tmp/up200.bin", timeout=120)
        client.exec_bg(f"curl -s -o /dev/null -m {k.PHASE_S + 5} --data-binary @/tmp/up200.bin {target.up_url()}")
        time.sleep(2)
        ul = ping()
        time.sleep(5)
        i_s, d_s, u_s = stats(idle), stats(dl), stats(ul)
        checks.row(phase="idle", rtt=i_s, p95=p95(idle))
        checks.row(phase="download", rtt=d_s, p95=p95(dl))
        checks.row(phase="upload", rtt=u_s, p95=p95(ul))
        agg_ok, prev, incomplete = True, 0.0, []
        for n in [int(x) for x in k.words("PARALLEL")]:
            t0 = time.time()
            w = client.out(f"for i in $(seq 1 {n}); do curl -s -o /dev/null -m {cfg.cap} -w '%{{size_download}} %{{time_total}}\\n' "
                           f"{target.down_url(cfg.bytes)} & done; wait", timeout=cfg.cap + 60)
            dt = time.time() - t0
            rows = [l.split() for l in w.splitlines() if l.strip()]
            got = sum(int(float(r[0])) for r in rows)
            done = sum(1 for r in rows if int(float(r[0])) >= cfg.bytes)
            agg = round(got * 8 / max(dt, 0.001) / 1e6, 2)
            checks.row(parallel=n, dir="down", agg_mbit=agg, elapsed_ms=int(dt * 1000), bytes=got, complete=done, flows=n)
            if done != n:
                incomplete.append(f"n={n}: {n - done} of {n} flows did not complete within {cfg.cap}s ({got} of {n * cfg.bytes} bytes)")
            else:
                if agg < 0.8 * prev:
                    agg_ok = False
                prev = agg
            checks.log(f"parallel {n} downloads: {agg} Mbit/s aggregate, {done}/{n} complete")
    dp, up = p95(dl), p95(ul)
    checks.record(f"idle p50 {i_s.get('median')} ms; download p95 {dp} ms; upload p95 {up} ms; parallel aggregate non-decreasing={int(agg_ok)}")
    if incomplete:
        checks.failed("parallel flows stalled: " + "; ".join(incomplete))
    elif agg_ok:
        checks.passed(f"every parallel flow completed and the aggregate does not fall as N rises over [{k.PARALLEL}]")
    else:
        checks.failed(f"parallel aggregate fell as N rose over [{k.PARALLEL}] although every flow completed; see the parallel rows")
    checks.assert_max("loaded RTT p95 during download", dp, "ms", "DOWN_P95_MAX_MS", k.DOWN_P95_MAX_MS)
    checks.assert_max("loaded RTT p95 during upload", up, "ms", "UP_P95_MAX_MS", k.UP_P95_MAX_MS)
