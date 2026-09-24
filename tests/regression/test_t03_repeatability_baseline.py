"""T03-repeatability-baseline (diagnostic): how many repetitions does a claim of a given size need on this stack?
N warm T04-style cells back to back (connect, 20 s idle floored to SURB_RAMP_WAIT, one download and one upload of
BYTES), nothing changed between them. Records n, median and stdev per direction and mde_pct, the minimum
detectable effect of a REPS-transfer median: the larger over the two directions of
2 * 1.96 * stdev / mean / sqrt(REPS) * 100. Above UNSTABLE_PCT it records UNSTABLE BASELINE; nothing consumes the
number, it is there so the thresholds' headroom can be judged. Runs third, before any gate reads a number.

Why: on the fleet, single-client deviations were as large as the medians and one exit varied 2x between two
ladders. Without this number a comparison is unfalsifiable, and the investigation's most expensive errors were
reading drift as an effect and an effect as drift."""
import json

from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import stats
from suitelib.target import curl_down, curl_up

TEST = "T03-repeatability-baseline"
KIND = "diagnostic"
GROUP = "preflight"
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
