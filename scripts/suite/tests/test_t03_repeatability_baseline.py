"""T03-repeatability-baseline (diagnostic): N unchanged warm T04 cells back to back on one stack; records medians,
spread and the minimum detectable effect at REPS transfers (mde_pct), so the headroom of the suite's absolute
thresholds can be judged for this stack. Flags UNSTABLE above UNSTABLE_PCT. Runs third so the stack's
repeatability is on record before any gate reads a number."""
import json

from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import stats
from suitelib.target import curl_down, curl_up

TEST = "T03-repeatability-baseline"
KIND = "diagnostic"
KNOBS = dict(N=q(10, 5), UNSTABLE_PCT=50)


def mde_pct(s, reps):
    """Minimum detectable effect (%) of a REPS-transfer median at this spread, 95 % two-sided."""
    if not s.get("n") or not s.get("mean"):
        return None
    return round(2 * 1.96 * s["stdev"] / max(s["mean"], 1e-9) / (reps ** 0.5) * 100, 1)


def test_repeatability_baseline(cfg, client, target, checks, knobs, stack_key):
    k = knobs
    down, up = [], []
    for i in range(1, k.N + 1):
        s = connect_or_fail(checks, client, cfg.dest, 20, label=f"rep {i}")
        if not s:
            continue
        with s:
            r = curl_down(client, target.ip, cfg.bytes, cfg.cap)
            w = curl_up(client, target.ip, cfg.bytes, cfg.cap)
        checks.row(rep=i, down=r, up=w)
        down.append(r["mbit"])
        up.append(w["mbit"])
    ds, us = stats(down), stats(up)
    m = [x for x in (mde_pct(ds, cfg.reps), mde_pct(us, cfg.reps)) if x is not None]
    band = {"n": ds.get("n", 0), "down_median": ds.get("median"), "down_stdev": ds.get("stdev"),
            "up_median": us.get("median"), "up_stdev": us.get("stdev"), "mde_pct": max(m) if m else 20, "reps_assumed": cfg.reps}
    checks.row(kind="summary", down=ds, up=us, band=band)
    mde = band["mde_pct"]
    checks.record(f"n={k.N} warm on stack {stack_key}: down median {ds.get('median')} stdev {ds.get('stdev')}; "
                  f"up median {us.get('median')} stdev {us.get('stdev')}; minimum detectable effect +-{mde}% at REPS={cfg.reps}")
    # A spread this wide is a finding about the stack: an old-version run measured +-279 %, and no threshold with
    # sane headroom can be trusted on a stack that cannot repeat its own numbers.
    if float(mde) > float(k.UNSTABLE_PCT):
        checks.record(f"UNSTABLE BASELINE: minimum detectable effect +-{mde}% exceeds UNSTABLE_PCT {k.UNSTABLE_PCT}%; this stack "
                      f"cannot repeat its own throughput, so read every threshold verdict in this run as weak evidence")
    print(json.dumps(band))
