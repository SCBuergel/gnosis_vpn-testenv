"""T17-latency-matrix (diagnostic): the real RTTs between the controlled nodes. N hoprd pings per ordered node
pair (min/median/mean/max/stdev) plus a peer survey from node 0 over its connected table. On one host the medians
are 2-3 ms and say little; the stdev tail is the signal (STDEV_MAX is declared, not asserted). With
SUITE_LATENCY_MAP="idx=ms ..." (a cell that injected a latency map) it gates: the node0->idx median must be within
LATENCY_TOL_MS of ms plus the node0->node1 median, or at least ms, or every rung above it is meaningless. The
promotion is `checks.kind = "gate"`, which the harness honours (conftest reads the checks object's kind before the
module's KIND), so a FAIL here fails the run.

Why: a few hundred pings identified the saturated relay of T01's forwarding probe by its jitter tail before that
probe existed, and the matrix tells a test author which impairment rungs are realistic."""
import time

from suitelib.stats import stats

TEST = "T17-latency-matrix"
KIND = "diagnostic"
KNOBS = dict(N=10, STDEV_MAX=25, SUITE_LATENCY_MAP="", LATENCY_TOL_MS=15)


def test_latency_matrix(cfg, live_cluster, checks, knobs):
    cluster = live_cluster
    k = knobs
    med = {}
    for i in range(cfg.cluster_size):
        for j in range(cfg.cluster_size):
            if i == j:
                continue
            v = []
            for _ in range(k.N):
                v.append(cluster.ping(i, j))
                time.sleep(0.2)
            s = stats(v)
            checks.row(**{"from": i, "to": j, "stats": s})
            med[(i, j)] = s.get("median")
            checks.log(f"node{i}->node{j} median {med[(i, j)]} ms stdev {s.get('stdev')}")
    d = cluster.api_json(0, "GET", "/api/v4/network/connected", default=[])
    lst = d if isinstance(d, list) else (d or {}).get("connected", [])
    peers = [{"address": p.get("address"), "avg_ms": p.get("averageLatency"), "probe_rate": p.get("probeRate")} for p in lst]
    checks.row(**{"kind": "peer_survey", "from": 0, "peers": peers, "latency_map": k.SUITE_LATENCY_MAP})
    if not k.SUITE_LATENCY_MAP:
        pairs = cfg.cluster_size * (cfg.cluster_size - 1)
        checks.record(f"no impairment configured: {pairs} pairs, medians " + " ".join(str(m) for m in med.values()))
        return
    # gate: every configured delay must show up in the measured medians (traffic to that node, from node 0)
    checks.kind = "gate"
    bad = []
    base = med.get((0, 1)) or 3
    for kv in k.SUITE_LATENCY_MAP.split():
        idx, want = kv.split("=")
        got = med.get((0, int(idx)))
        if got is None:
            bad.append(f"node{idx}:no-measurement")
        elif not (abs(got - (float(want) + base)) <= k.LATENCY_TOL_MS or got >= float(want)):
            bad.append(f"node{idx}:want>={want}ms,got{got}ms")
    if not bad:
        checks.passed(f"impairment verified: map '{k.SUITE_LATENCY_MAP}' visible in the measured pair medians")
    else:
        checks.failed(f"impairment NOT applied: {' '.join(bad)} (map '{k.SUITE_LATENCY_MAP}') - rungs above this are meaningless")
