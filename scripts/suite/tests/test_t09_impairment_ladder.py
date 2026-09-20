"""T09-impairment-ladder (gate): the exit's return relays are impaired live with tc netem on the host (no cluster
restart, no chain rebuild). ladder: rungs of delay on EVERY relay (equal) vs on ONE relay (gap); far: one relay at
FAR ms. Gate: every equal-latency cell completes with zero reassembly failures and zero reconnects. Gap and far
cells are recorded. The stream runs FIRST on the fresh session (after bulk transfers it showed 9-54 reassembly
failures and up to 71 % loss at 0 ms; that state is T06's dl-after-bulk arm now).
Relay-set membership is varied by impairment, never by the channel API: a close/reopen inside one run leaves
the channel PendingToClose and the exit with no usable return relay. Needs root for tc."""
import os

from suitelib.client import ConnectFailed
from suitelib.config import q
from suitelib.target import summary_row, transfer_series

TEST = "T09-impairment-ladder"
KIND = "gate"
KNOBS = dict(RUNGS=q("0 25 50", "0 25"), FAR=100, STREAM_S=q(120, 90))


def test_impairment_ladder(cfg, run, client, cluster, target, checks, knobs):
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
