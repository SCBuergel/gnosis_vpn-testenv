"""T30-hopcount-ab (runbook): T04 at 1 hop (DEST) and 0 hops (DEST-h0). Needs HOPS0_ALSO=1 at gen-config and
CLIENT_EXTRA_ARGS=--allow-insecure at client start; skips otherwise. 0-hop is the upper bound."""
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
