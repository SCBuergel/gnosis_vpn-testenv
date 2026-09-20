"""T04-fixed-throughput (the core gate): one session, REPS x (download, upload) against the in-cluster target,
per-second stall detection, client-log error counters, telemetry deltas.
Scoring: the error counters are absolute (a healthy stack has zero reassembly failures and zero reconnects); the
medians are held against the absolute floors DOWN_MIN_MBIT / UP_MIN_MBIT. Calibration: the reference stack
measures 10.9 down (stdev 0.45) and 13.0 up (stdev 0.69) over ten warm sessions; the 2026-09 relay
decode-concurrency regression halved throughput, so 7 catches a halving and clears host noise (+-12 %)."""
from suitelib.client import connect_or_fail
from suitelib.target import summary_row, transfer_series

TEST = "T04-fixed-throughput"
KIND = "gate"
KNOBS = dict(WAIT_AFTER_CONNECT=0, LABEL="t04", DOWN_MIN_MBIT=7, UP_MIN_MBIT=7)
UNDECODABLE = 'hopr_packet_rejected_count{reason="undecodable"}'


def test_fixed_throughput(cfg, client, cluster, target, checks, knobs):
    k = knobs
    undec0 = client.telemetry_metric(UNDECODABLE)
    s = connect_or_fail(checks, client, cfg.dest, k.WAIT_AFTER_CONNECT)
    if not s:
        return
    with s:
        with cluster.sampler(checks.run, k.LABEL, 1, [client.name, cfg.server]):
            summary = transfer_series(checks, client, k.LABEL, target.ip, cfg.reps, cfg.bytes, cfg.cap)
        errs = s.errors()
        s.save_log(k.LABEL)
    undec1 = client.telemetry_metric(UNDECODABLE)
    checks.row(label=k.LABEL, kind="summary", summary=summary_row(summary), errors=errs, connect_ms=s.connect_ms,
               wait_after_connect=k.WAIT_AFTER_CONNECT, undecodable_before=undec0, undecodable_after=undec1)
    dc, uc = summary["down_complete"], summary["up_complete"]
    detail = (f"reassembly={errs['reassembly_failed']} reconnects={errs['reconnects']} (tunnel-ping timeouts "
              f"{errs['ping_timeouts']}) discards={errs['frame_discarded']} decap={errs['decap_error']}")
    if dc == cfg.reps and uc == cfg.reps and errs["reassembly_failed"] == 0 and errs["reconnects"] == 0:
        checks.passed(f"all {cfg.reps} transfers complete both ways; {detail}")
    else:
        checks.failed(f"complete {dc}/{cfg.reps} down {uc}/{cfg.reps} up; {detail}")
    checks.assert_min("download median", summary["down_median"], "Mbit/s", "DOWN_MIN_MBIT", k.DOWN_MIN_MBIT)
    checks.assert_min("upload median", summary["up_median"], "Mbit/s", "UP_MIN_MBIT", k.UP_MIN_MBIT)
