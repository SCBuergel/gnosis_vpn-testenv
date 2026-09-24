"""T15-warmup-knee (diagnostic): how long must a fresh session idle before it carries full-rate traffic? One
download of BYTES per delay in DELAYS after a fresh connect (ramp_wait_opt_out: this test is the delay sweep).
Records the knee, the first delay at which the first download reaches KNEE_FRAC x the best delay's rate, and flags
a knee beyond KNEE_MAX_S as a ramp defect. Nothing is asserted: a fresh session always warms up (the exit's shaper
starts at its initial rate and the readiness gate has to clear), so 'flat across the sweep' is not a healthy-stack
property and the gate is T07.

Why: the knee sat at ~240 s on the broken client, exactly the ramp's length; it turns 'cold starts are bad' into a
number a developer can match against a constant in the code."""
from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import stats
from suitelib.target import curl_down

TEST = "T15-warmup-knee"
KIND = "diagnostic"
GROUP = "throughput"
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
    if not measured:
        checks.row(kind="summary", stats=st, delays=k.DELAYS, knee_s=None)
        return checks.record(f"no delay measured (every connect failed over [{k.DELAYS}]); no knee to report")
    best = max(measured) if measured else 0
    knee = next((d for d, v in zip(delays, vals) if v is not None and best > 0 and v >= k.KNEE_FRAC * best), "beyond-sweep")
    checks.row(kind="summary", stats=st, delays=k.DELAYS, knee_s=knee, first_transfer_mbit=" ".join(str(v) for v in vals))
    checks.record(f"warm-up knee at {knee}s idle (first transfer reaches {k.KNEE_FRAC} of best); Mbit/s over delays [{k.DELAYS}]: "
                  + " ".join(str(v) for v in vals))
    if knee == "beyond-sweep" or float(knee) > k.KNEE_MAX_S:
        checks.record(f"knee is beyond {k.KNEE_MAX_S}s - a warm-up that long is a ramp defect, compare against T07-cold-start")
