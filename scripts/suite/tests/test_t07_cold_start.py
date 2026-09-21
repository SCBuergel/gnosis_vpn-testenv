"""T07-cold-start (gate): one arm measured immediately after connect, one after WARM s; also records the exit-side
session SURB target before load. WARM is 25 s, past client 0.96.2's 20 s SURB ramp; it was briefly 75 s and that
was harmful (a long post-connect idle lets the return-path SURBs expire, so the first transfer after it dies with
a tunnel-ping reconnect, 8/8 on two hoprd versions). The cold arm opts out of the ramp wait: measuring the ramp is
its job. The cold-start signal is a COLLAPSE (no completion, decap errors, a reconnect), not that the cold arm is
slower: a fresh session measures the SURB ramp, whose first transfer is ~0.5x steady state by design (0.48, 0.34
and 0.25 measured on clean cold starts), so the ratio is recorded with COLD_WARM_FIRST_RATIO as a reference.
Gate: zero reconnects in both arms, the cold arm completes at least as many downloads as the warm one, and the
cold arm's decapsulation errors stay under max(COLD_DECAP_FLOOR, COLD_DECAP_MULT x the warm arm's). Medians are
recorded, not gated."""
from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.target import summary_row, transfer_series

TEST = "T07-cold-start"
KIND = "gate"
KNOBS = dict(WARM=q(25, 25), COLD_WARM_FIRST_RATIO=0.35, COLD_DECAP_MULT=2, COLD_DECAP_FLOOR=5)


def test_cold_start(cfg, client, cluster, target, checks, knobs):
    k = knobs

    def run_arm(arm, wait):
        s = connect_or_fail(checks, client, cfg.dest, wait, label=arm, ramp_wait_opt_out=(wait == 0))
        if not s:
            return None
        with s:
            tgt = 0.0
            for line in cluster.metrics(0).splitlines():
                if line.startswith("hopr_session_surb_target_buffer{"):
                    tgt = float(line.split()[1])
            summ = transfer_series(checks, client, f"t07-{arm}", target.ip, cfg.q(cfg.reps, 1), cfg.bytes, cfg.cap)
            errs = s.errors()
            s.save_log(f"t07-{arm}")
        checks.row(arm=arm, wait=wait, summary=summary_row(summ), errors=errs, exit_surb_target_before_load=tgt)
        return summ, errs, tgt

    cold = run_arm("cold", 0)
    warm = run_arm("warm", k.WARM)
    if not cold or not warm:
        return
    cs, ce, ct = cold
    ws, we, _ = warm
    # the first transfer of each arm is the cold-start signal; medians hide it behind the recovered reps
    cfirst = cs["down"][0]["mbit"] if cs["down"] else 0
    wfirst = ws["down"][0]["mbit"] if ws["down"] else 0
    msg = (f"cold: first download {cfirst} Mbit/s, median {cs['down_median']}, complete {cs['down_complete']}, decap {ce['decap_error']}, "
           f"reconnects {ce['reconnects']} (tunnel-ping timeouts {ce['ping_timeouts']}); warm: first {wfirst}, median {ws['down_median']}, "
           f"complete {ws['down_complete']}, decap {we['decap_error']}, reconnects {we['reconnects']} (tunnel-ping timeouts "
           f"{we['ping_timeouts']}); exit SURB target at cold load start: {ct}")
    ratio = round(cfirst / max(wfirst, 0.001), 2)
    checks.record(f"cold/warm first-transfer ratio {ratio} (the SURB ramp; 0.48, 0.34 and 0.25 measured on clean cold starts, so it is "
                  f"recorded, not gated; COLD_WARM_FIRST_RATIO={k.COLD_WARM_FIRST_RATIO} is the value below which it is worth a look)")
    ok = (ce["reconnects"] == 0 and we["reconnects"] == 0 and cs["down_complete"] >= ws["down_complete"]
          and ce["decap_error"] <= max(k.COLD_DECAP_FLOOR, k.COLD_DECAP_MULT * we["decap_error"]))
    checks.verdict(ok, msg)
