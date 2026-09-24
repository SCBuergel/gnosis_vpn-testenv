"""T22-concurrent-clients (gate): how does the stack behave as simultaneous clients increase? N client containers
download BYTES through the same exit at the same time, over the rungs in LADDER (clamped to the clients running;
SKIP below 2). Every client is warmed with WARMUP_BYTES before the measured transfer, or each rung would measure the
SURB ramp (T07) instead of concurrency. Every client's rung is diagnosed: warm-up result, whether it was Connected
right before the measured transfer, reconnects and tunnel-ping timeouts, and any Disconnect command it received
(the deadman bug's only trace was one such line in 42 000 lines of debug).

Per rung: FAIL if any client fails to connect or does not complete within CAP, naming per incomplete client its
bytes, HTTP code and the diagnosis above. Across the ladder, only when every rung completed: PASS iff no higher
rung's aggregate falls more than TOL_PCT below any lower rung's (one-sided: aggregate rising with concurrency is
the healthy shape on a stack where one client cannot saturate the exit; 10.6, 13.7, 16.1 Mbit/s at 1, 2, 4
clients, and the earlier symmetric rule failed that ladder) and the top rung's aggregate is at least AGG_MIN_MBIT.
Per-client throughput is expected to fall with N; fairness is recorded, not asserted.

Why: the fleet's ladders found a hard break at a specific client count and a plateau far under the no-VPN baseline
(38-49 Mbit/s aggregate from n~7, admissions stopping at 14-16, zero-byte transfers from n=12, while the same
16 clients shared ~220 Mbit/s without the VPN). Setup: CLIENT_COUNT=N on a cluster created with
EXTRA_IDENTITIES=N, then `just clients-start`."""
import time
from concurrent.futures import ThreadPoolExecutor

from suitelib.client import ConnectFailed
from suitelib.verdicts import utc_now
from suitelib.target import curl_down

TEST = "T22-concurrent-clients"
KIND = "gate"
KNOBS = dict(LADDER="1 2 4", TOL_PCT=25, WARMUP=1, WARMUP_BYTES=500000, AGG_MIN_MBIT=8, CAP=None)


def test_concurrent_clients(cfg, run, clients, target, checks, knobs):
    k = knobs
    cap = int(k.CAP) if k.CAP else cfg.cap
    avail = len(clients)
    if avail < 2:
        checks.skip(f"only {avail} client container(s) running; need at least 2 (CLIENT_COUNT=N + just clients-start)")
    rungs = [int(n) for n in k.words("LADDER") if 1 <= int(n) <= avail]     # 0 or a negative rung would build an empty group
    if not rungs:
        checks.skip(f"no ladder rung fits {avail} running client(s)")

    def cleanup():
        for c in clients:
            c.disconnect()

    agg_by = {}

    def run_rung(n):
        group = clients[:n]
        since = utc_now()
        for i, c in enumerate(group, 1):
            try:
                c.connect(cfg.dest, 5)
            except ConnectFailed as e:
                checks.failed(f"n={n}: client {i} failed to connect: {e}")
                cleanup()
                return False
        warm = {}
        if k.WARMUP == 1:   # warm every client past the ramp before measuring, in parallel so the rung is not serialised
            with ThreadPoolExecutor(n) as ex:
                warm = dict(zip(range(1, n + 1), ex.map(lambda c: curl_down(c, target.ip, k.WARMUP_BYTES, cap), group)))
            time.sleep(3)
        conn = {i: c.is_connected() for i, c in enumerate(group, 1)}
        with ThreadPoolExecutor(n) as ex:
            res = dict(zip(range(1, n + 1), ex.map(lambda c: curl_down(c, target.ip, cfg.bytes, cap), group)))
        errs, dcmd = {}, {}
        for i, c in enumerate(group, 1):
            errs[i] = c.log_errors(since)
            dcmd[i] = c.count_log(since, r"received socket command.*command=Disconnect")
            c.save_log(f"t22-n{n}-c{i}", since)
        cleanup()
        why, incomplete, mbits = [], 0, []
        for i in range(1, n + 1):
            j = res[i]
            mbits.append(j["mbit"])
            checks.row(n=n, client=i, result=j, warmup=warm.get(i, {}), connected_before=conn[i], errors=errs[i], disconnect_cmds=dcmd[i])
            if not j["complete"]:
                incomplete += 1
                why.append(f"client {i}: {j['bytes']} bytes (http {j['code']}), warm-up {warm.get(i, {}).get('bytes')} bytes, "
                           f"{'Connected' if conn[i] else 'NOT CONNECTED'} before the transfer, reconnects {errs[i]['reconnects']} "
                           f"(tunnel-ping timeouts {errs[i]['ping_timeouts']}), Disconnect commands {dcmd[i]}")
        agg = round(sum(mbits), 3)
        fair = round(min(mbits) / (sum(mbits) / len(mbits)), 2) if mbits and sum(mbits) > 0 else 0
        agg_by[n] = agg
        checks.row(n=n, kind="rung", aggregate_mbit=agg, fairness=fair, incomplete=incomplete)
        if incomplete:
            checks.failed(f"n={n}: {incomplete}/{n} client(s) did not complete the transfer within {cap}s (aggregate {agg} Mbit/s, "
                          f"warmup={k.WARMUP}) - " + "; ".join(why))
            return False
        checks.record(f"n={n}: aggregate {agg} Mbit/s over {mbits}, slowest/mean {fair}, all {n} complete, reconnects "
                      f"{sum(errs[i]['reconnects'] for i in errs)}")
        return True

    try:
        ok = True
        for n in rungs:
            if not run_rung(n):
                ok = False
                break
    finally:
        cleanup()
    if not ok:
        return
    # (b) aggregate must not collapse as clients are added: every higher rung against every lower rung, one-sided
    worst_drop, worst_pair = 0.0, ""
    for n in rungs:
        for m in rungs:
            if m >= n:
                continue
            a, b = agg_by[m], agg_by[n]
            d = round((a - b) / a * 100, 1) if a > 0 else 0
            if d > worst_drop:
                worst_drop, worst_pair = d, f"n={m} -> n={n}"
    summary = "  ".join(f"n={n} {agg_by[n]}" for n in rungs)
    if worst_drop <= k.TOL_PCT:
        checks.passed(f"aggregate holds or grows with concurrency up to {rungs[-1]} clients: {summary} Mbit/s (worst step "
                      f"{worst_pair or 'none'} {worst_drop}%, tolerance {k.TOL_PCT}%)")
    else:
        checks.failed(f"aggregate collapses with concurrency: {summary} Mbit/s ({worst_pair} drops {worst_drop}%, tolerance {k.TOL_PCT}%)")
    checks.assert_min(f"aggregate at n={rungs[-1]}", agg_by[rungs[-1]], "Mbit/s", "AGG_MIN_MBIT", k.AGG_MIN_MBIT)
