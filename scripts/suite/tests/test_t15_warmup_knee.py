"""T15-warmup-knee (diagnostic): first-transfer throughput vs idle time after connect. A fresh session always warms
up (the exit's shaper starts at its initial egress rate and the readiness gate has to clear), so "flat across the
sweep" is not a healthy-stack property. What this measures is the KNEE: the idle time after which the first
transfer reaches KNEE_FRAC of the best rung. T07 is the gate; this says how long the warm-up lasts.
This test IS the delay sweep, so it opts out of the ramp wait."""
from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import stats
from suitelib.target import curl_down

TEST = "T15-warmup-knee"
KIND = "diagnostic"
KNOBS = dict(DELAYS=q("0 5 15 30 60", "0 5 20"), KNEE_FRAC=0.8, KNEE_MAX_S=30)


def test_warmup_knee(cfg, client, target, checks, knobs):
    k = knobs
    delays, vals = k.words("DELAYS"), []
    for d in delays:
        s = connect_or_fail(checks, client, cfg.dest, int(d), label=f"delay {d}", ramp_wait_opt_out=True)
        if not s:
            vals.append(None)      # keep the delay/value pairing; a failed delay is a hole, not a shift
            continue
        with s:
            client.persec_start(f"t15-{d}")
            r = curl_down(client, target.ip, cfg.bytes, cfg.cap)
            client.persec_stop()
            e = s.errors()
        checks.row(delay=int(d), first_download=r, errors=e, stall_s=client.persec_stall(f"t15-{d}", "rx"))
        vals.append(r["mbit"])
        checks.log(f"delay {d} s -> {r['mbit']} Mbit/s complete={r['complete']}")
    st = stats(vals)
    measured = [v for v in vals if v is not None]
    best = max(measured) if measured else 0
    knee = next((d for d, v in zip(delays, vals) if v is not None and best > 0 and v >= k.KNEE_FRAC * best), "beyond-sweep")
    checks.row(kind="summary", stats=st, delays=k.DELAYS, knee_s=knee, first_transfer_mbit=" ".join(str(v) for v in vals))
    checks.record(f"warm-up knee at {knee}s idle (first transfer reaches {k.KNEE_FRAC} of best); Mbit/s over delays [{k.DELAYS}]: "
                  + " ".join(str(v) for v in vals))
    if knee == "beyond-sweep" or float(knee) > k.KNEE_MAX_S:
        checks.record(f"knee is beyond {k.KNEE_MAX_S}s - a warm-up that long is a ramp defect, compare against T07-cold-start")
