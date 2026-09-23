"""T04-fixed-throughput (the core gate): one session; REPS cycles of [for each size in SIZES_MIB: download, then
upload] against the in-cluster target, each transfer capped at CAP s. Per transfer: bytes, elapsed, time to first
byte, Mbit/s, HTTP status, complete or truncated, and the longest zero-progress second from per-second sampling (a
5 s stall and a uniformly slow transfer have the same Mbit/s; the stall is what the user feels). After disconnect:
the four client-log error counters over the session and the client's undecodable counter before and after.

Pass iff every transfer completes in both directions, reassembly_failed = 0 and reconnects = 0 over the session,
and the download and upload medians at the largest size are at least DOWN_MIN_MBIT / UP_MIN_MBIT. Frame discards,
decapsulation errors and the smaller sizes' medians are recorded, not asserted. Calibration: the reference stack
reads 10.9 down (stdev 0.45) and 13.0 up (stdev 0.69) over ten warm 10 MB sessions; the 2026-09 relay
decode-concurrency regression halved throughput, so 7 catches a halving and clears host noise (+-12 %).

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
KNOBS = dict(WAIT_AFTER_CONNECT=0, LABEL="t04", SIZES_MIB=q("1 10 50", "1 10"), DOWN_MIN_MBIT=7, UP_MIN_MBIT=7)
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
    n, dc, uc = summary["n"], summary["down_complete"], summary["up_complete"]
    per_size = " ".join(f"{b // MIB}MiB:{v['down_median']}/{v['up_median']}" for b, v in summary["by_size"].items())
    detail = (f"reassembly={errs['reassembly_failed']} reconnects={errs['reconnects']} (tunnel-ping timeouts "
              f"{errs['ping_timeouts']}) discards={errs['frame_discarded']} decap={errs['decap_error']}; "
              f"down/up Mbit/s per size {per_size}")
    if dc == n and uc == n and errs["reassembly_failed"] == 0 and errs["reconnects"] == 0:
        checks.passed(f"all {n} transfers complete both ways; {detail}")
    else:
        checks.failed(f"complete {dc}/{n} down {uc}/{n} up; {detail}")
    # the floors are calibrated on a bulk transfer: judge them at the largest size, where ramp and TTFB weigh least
    largest = summary["by_size"][max(summary["by_size"])]
    checks.assert_min(f"download median at {max(sizes) // MIB} MiB", largest["down_median"], "Mbit/s", "DOWN_MIN_MBIT", k.DOWN_MIN_MBIT)
    checks.assert_min(f"upload median at {max(sizes) // MIB} MiB", largest["up_median"], "Mbit/s", "UP_MIN_MBIT", k.UP_MIN_MBIT)
