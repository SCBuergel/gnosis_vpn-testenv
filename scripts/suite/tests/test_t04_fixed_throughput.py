"""T04-fixed-throughput (the core gate): one session; REPS cycles of [for each size in SIZES_MIB: download, then
upload] against the in-cluster target, each transfer capped at CAP s. Per transfer: bytes, elapsed, time to first
byte, Mbit/s, HTTP status, complete or truncated, and the longest zero-progress second from per-second sampling (a
5 s stall and a uniformly slow transfer have the same Mbit/s; the stall is what the user feels). After disconnect:
the four client-log error counters over the session and the client's undecodable counter before and after.

Three kinds of verdict line (one per size, one for the counters, two floors). Per size: PASS iff every transfer of that size completes in both directions (the line carries the
medians and the longest zero-progress second, so a size that stalls to the cap reads as a stall). Session
counters: PASS iff reassembly_failed = 0 and reconnects = 0. Floors: the download and upload medians at FLOOR_MIB
(the largest size in the cycle at or under it) must be at least DOWN_MIN_MBIT / UP_MIN_MBIT. Frame discards,
decapsulation errors and the other sizes' medians are recorded, not asserted. Calibration: the floors come from
ten warm 10 MB sessions on the reference stack, 10.9 down (stdev 0.45) and 13.0 up (stdev 0.69); the 2026-09
relay decode-concurrency regression halved throughput, so 7 catches a halving and clears host noise (+-12 %). No
size above 10 MiB has been calibrated as a rate, which is why the floors are not judged there.

Open finding, 2026-09-23 (run r3t04; hoprd 4.1.3 from the hoprd-4arb tree with its inert env-toggle patch,
client 0.96.3-a974f5fc glibc image, server 0.7.0): two of three 50 MiB downloads ran at 10-19 Mbit/s for
17-23 s (25-30 MB), hit a burst of frame discards (355 and 163 in ten seconds), then delivered nothing until the
90 s cap; five tunnel-ping timeouts and one reconnect followed, and the third completed on the fresh session.
1 and 10 MiB and every 50 MiB upload bar the one on the dead session completed; the exit and relays logged
nothing beyond routine SURB evictions. That is the download-side collapse of exploration/deep-2026-09-22,
reproduced by a plain TCP download that runs longer than a 10 MB one. Until it is fixed the full-length run is
red on the 50 MiB completion and the session counters; --fast (SIZES_MIB "1 10") passes. When an issue exists,
the 50 MiB completion can become an XFAIL naming it (discard burst, zero progress to the cap, tunnel-ping
reconnect) so it reports XPASS the day the defect is gone; a bare "known failure" is not that.

Why: this harness produced every cell of the investigation, and its three outputs do not substitute for each
other: completions caught truncation, Mbit/s caught the 2x relay regression, the error counters caught the client
regression (uploads at 10 Mbit/s while downloads died at 0.37; a mean would have read 5 and hidden both). curl runs
inside the client container, where the tunnel routes are; the target is in-cluster so no internet path or CDN
rate limit enters the number."""
from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.target import summary_row, transfer_series

TEST = "T04-fixed-throughput"
KIND = "gate"
KNOBS = dict(WAIT_AFTER_CONNECT=0, LABEL="t04", SIZES_MIB=q("1 10 50", "1 10"), FLOOR_MIB=10, DOWN_MIN_MBIT=7, UP_MIN_MBIT=7)
MIB = 1 << 20
UNDECODABLE = 'hopr_packet_rejected_count{reason="undecodable"}'


def test_fixed_throughput(cfg, client, cluster, target, checks, knobs):
    k = knobs
    undec0 = client.telemetry_metric(UNDECODABLE)
    s = connect_or_fail(checks, client, cfg.dest, k.WAIT_AFTER_CONNECT)
    if not s:
        return
    with s:
        with cluster.sampler(checks.run, k.LABEL, 1, [client.name, cfg.server]):
            sizes = [int(float(m) * MIB) for m in k.words("SIZES_MIB")]
            summary = transfer_series(checks, client, k.LABEL, target.ip, cfg.reps, cfg.bytes, cfg.cap, sizes=sizes)
        errs = s.errors()
        s.save_log(k.LABEL)
    undec1 = client.telemetry_metric(UNDECODABLE)
    checks.row(label=k.LABEL, kind="summary", summary=summary_row(summary), errors=errs, connect_ms=s.connect_ms,
               wait_after_connect=k.WAIT_AFTER_CONNECT, undecodable_before=undec0, undecodable_after=undec1)
    # completion per size, as its own verdict: a size whose transfers stall to the cap reads as a stall, not as a rate
    for b, v in summary["by_size"].items():
        what = (f"{b // MIB} MiB: {v['down_complete']}/{v['n']} down and {v['up_complete']}/{v['n']} up complete, "
                f"medians {v['down_median']}/{v['up_median']} Mbit/s, longest zero-progress {v['stall_max_s']} s")
        (checks.passed if v["down_complete"] == v["n"] and v["up_complete"] == v["n"] else checks.failed)(what)
    counters = (f"session counters: reassembly={errs['reassembly_failed']} reconnects={errs['reconnects']} (tunnel-ping timeouts "
                f"{errs['ping_timeouts']}) discards={errs['frame_discarded']} decap={errs['decap_error']}")
    (checks.passed if errs["reassembly_failed"] == 0 and errs["reconnects"] == 0 else checks.failed)(counters)
    # the floors are calibrated at FLOOR_MIB (10 warm 10 MB sessions) and judged there; a larger size is only a
    # completion check above, because a rate measured through a stall is a stall, not a throughput
    floor_size = max([b for b in sizes if b <= k.FLOOR_MIB * MIB] or [min(sizes)])
    at = summary["by_size"][floor_size]
    checks.assert_min(f"download median at {floor_size // MIB} MiB", at["down_median"], "Mbit/s", "DOWN_MIN_MBIT", k.DOWN_MIN_MBIT)
    checks.assert_min(f"upload median at {floor_size // MIB} MiB", at["up_median"], "Mbit/s", "UP_MIN_MBIT", k.UP_MIN_MBIT)
