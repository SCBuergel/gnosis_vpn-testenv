"""T30-hopcount-ab (runbook): how much of the loss and latency is the relay hop itself? REPS (--fast 2) T04-style
transfers at 1 hop (DEST) and at 0 hops (DEST-h0), same client, exit and target. Needs HOPS0_ALSO=1 at gen-config
and CLIENT_EXTRA_ARGS=--allow-insecure at client start; SKIP otherwise. PASS iff the 0-hop download median is at
least the 1-hop one; WARN otherwise.

Why: the cheapest way to separate 'this exit is slow' from 'this path is slow', and it cleared an exit host of a
fault (0-hop 10.3-12.1 down / 21.2-22.5 up at 21 ms, the same exit at 1 hop 6.1-7.1 / 7.2-8.5 at 52 ms).
Production refuses --allow-insecure ('routing mode not allowed'), so this belongs to testenv and self-hosted exits."""
from suitelib.client import connect_or_fail
from suitelib.target import summary_row, transfer_series

TEST = "T30-hopcount-ab"
KIND = "runbook"
KNOBS = {}


def test_hopcount_ab(cfg, client, target, checks, knobs):
    h0 = f"{cfg.dest}-h0"
    if not client.dest_health_line(h0):
        checks.skip(f"no 0-hop destination {h0} (set HOPS0_ALSO=1 CLIENT_EXTRA_ARGS=--allow-insecure)")
    res = []
    for d in (cfg.dest, h0):
        s = connect_or_fail(checks, client, d, 15, label=f"connect {d}")
        if not s:
            continue
        with s:
            summ = transfer_series(checks, client, f"t30-{d}", target.ip, cfg.q(cfg.reps, 2), cfg.bytes, cfg.cap)
            e = s.errors()
        checks.row(dest=d, summary=summary_row(summ), errors=e)
        res.append(summ["down_median"])
    if len(res) != 2:
        return
    if res[1] >= res[0]:
        checks.passed(f"1-hop down {res[0]} Mbit/s, 0-hop down {res[1]} Mbit/s (0-hop is the upper bound)")
    else:
        checks.warn(f"0-hop down {res[1]} < 1-hop {res[0]} Mbit/s")
