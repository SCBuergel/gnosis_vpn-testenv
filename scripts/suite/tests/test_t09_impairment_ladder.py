"""T09-impairment-ladder (gate): with a fixed forward path, how does the set of relays the exit can use for return
traffic affect throughput and integrity? The exit's two return relays are impaired live with tc netem on the host
toward their P2P ports: no cluster restart, no channel change (a close/reopen inside one run leaves the channel
PendingToClose and the exit with no usable return relay, wedging every later test). Cells, each on a fresh
session: equal-<ms> delays both relays by a rung of RUNGS, gap-<ms> delays relay 1 only, far-<FAR>ms pushes relay
2 alone to FAR. Each cell runs a 3 Mbit/s download stream of STREAM_S first, then 3 (--fast 2) T04-style transfer
reps. Needs root for tc; SKIP below CLUSTER_SIZE 3.

Pass iff every equal cell has reassembly_failed = 0 and reconnects = 0. Gap and far cells, stream loss and
transfer completion are recorded, not asserted. The stream runs first because after bulk transfers it showed 9-54
reassembly failures and up to 71 % loss at 0 ms in three runs on hoprd 4.1.3 while T06's fresh-session stream read
0.2 %; that post-bulk state is T06's dl-after-bulk arm now.

Why: the first self-hosted exit's poor 1-hop performance was the default strategy spreading SURBs over relays with
a 20-340 ms RTT mix: 0.2 Mbit/s and 300 discards, against 4.6-6.3 Mbit/s pinned to one near relay. One far relay
alone only halved downloads; near plus far together collapsed uploads to 0.34 Mbit/s with 158 reassembly failures,
and two far relays of equal RTT carried 3 Mbit/s cleanly. RTT mismatch is the defect, not distance. CLUSTER_LATENCY
bakes a latency map into the localcluster for the same axis without tc."""
import os

from suitelib.client import ConnectFailed
from suitelib.config import q
from suitelib.target import summary_row, transfer_series

TEST = "T09-impairment-ladder"
KIND = "gate"
KNOBS = dict(RUNGS=q("0 25 50", "0 25"), FAR=100, STREAM_S=q(120, 90))
TIMEOUT = lambda k: (2 * len(k.words("RUNGS")) + 1) * (k.STREAM_S + 900)   # seconds; the harness fails the test past this


def test_impairment_ladder(cfg, run, client, live_cluster, target, checks, knobs):
    cluster = live_cluster
    k = knobs
    if cfg.cluster_size < 3:
        checks.skip("needs CLUSTER_SIZE>=3 (exit + two relays)")
    if os.geteuid() != 0:
        checks.skip("needs root for tc netem")
    relays = list(range(1, cfg.cluster_size))

    def cell(label, equal):
        try:
            s = client.connect(cfg.dest, 15)
        except ConnectFailed as e:
            if equal:
                checks.failed(f"{label}: connect failed: {e}")
            else:
                checks.record(f"{label}: connect failed: {e}")
            return
        with s:
            client.probe("streamprobe", f"t09-{label}.json", timeout=k.STREAM_S + 90, mode="dl", host=target.ip, port=target.stream_port,
                         rate_mbit=3, duration=k.STREAM_S, size=1200, iface=s.iface)
            summ = transfer_series(checks, client, f"t09-{label}", target.ip, cfg.q(3, 2), cfg.bytes, cfg.cap)
            j = run.read_json(f"t09-{label}.json", {})
            e = s.errors()
        checks.row(cell=label, equal=int(equal), summary=summary_row(summ), stream=j, errors=e)
        msg = (f"{label}: down {summ['down_median']} up {summ['up_median']} Mbit/s, complete {summ['down_complete']}/{summ['up_complete']}, "
               f"stream loss {j.get('loss_pct')}%, discards {e['frame_discarded']}, reassembly {e['reassembly_failed']}, "
               f"reconnects {e['reconnects']} (tunnel-ping timeouts {e['ping_timeouts']})")
        if equal:
            checks.verdict(e["reassembly_failed"] == 0 and e["reconnects"] == 0, msg)
        else:
            checks.record(msg)

    try:
        for ms in k.numbers("RUNGS"):
            ms = int(ms) if ms == int(ms) else ms
            cluster.netem_apply({r: ms for r in relays} if ms else {})
            cell(f"equal-{ms}ms", True)
            if ms:
                cluster.netem_apply({1: ms})
                cell(f"gap-{ms}ms", False)
        cluster.netem_apply({2: k.FAR})
        cell(f"far-{k.FAR}ms", False)
    finally:
        cluster.netem_clear()
