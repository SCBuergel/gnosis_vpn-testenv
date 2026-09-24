"""Offline checks for suitelib (no docker, no network): pytest tests/selftest (just suite-selftest)."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from suitelib import tomlcfg  # noqa: E402
from suitelib.client import count_log_errors, telemetry_sum  # noqa: E402
from suitelib.config import Config, q  # noqa: E402
from suitelib.verdicts import Checks, RunDir  # noqa: E402
from suitelib.stats import p95, stats  # noqa: E402
from suitelib.target import _curl_result  # noqa: E402


@pytest.fixture
def rundir(tmp_path):
    return RunDir(tmp_path / "run", cell="c1")


def last_verdict(rundir):
    return json.loads(open(rundir.verdicts_file).read().splitlines()[-1])


def test_stats_median_and_stdev():
    s = stats([1, 2, 3, 4, 5])
    assert s["median"] == 3 and s["n"] == 5 and s["stdev"] == pytest.approx(1.414, abs=1e-3) and s["mean"] == 3


def test_stats_ignores_na():
    assert stats([4, "NA", 6, None, ""])["n"] == 2


def test_p95_by_rank():
    assert p95(list(range(1, 101))) == 96


def test_row_writes_one_json_line(rundir):
    rundir.row("T99", a=1, b="text", c={"x": 2})
    r = rundir.rows("T99")[-1]
    assert r["test"] == "T99" and r["cell"] == "c1" and r["a"] == 1 and r["b"] == "text" and r["c"]["x"] == 2


def test_gate_fail_concludes_as_pytest_failure(rundir):
    c = Checks(rundir, "T99", "gate")
    c.failed("broken")
    assert last_verdict(rundir)["status"] == "FAIL"
    with pytest.raises(pytest.fail.Exception):
        c.conclude()


def test_diagnostic_fail_is_downgraded_to_warn(rundir):
    c = Checks(rundir, "T99", "diagnostic")
    c.failed("not a gate")
    v = last_verdict(rundir)
    assert v["status"] == "WARN" and v["kind"] == "diagnostic"
    c.conclude()   # never raises


def test_record_never_fails(rundir):
    c = Checks(rundir, "T99", "gate")
    c.record("a measurement")
    assert last_verdict(rundir)["status"] == "RECORDED"
    c.conclude()


def test_xfail_and_xpass(rundir):
    """XFAIL holds -> recorded, no failure; XPASS on a gate -> a failure, so a stale tag reddens the run."""
    c = Checks(rundir, "T99", "gate")
    c.xfail("hoprnet#8392", True, "defect reproduced")
    assert last_verdict(rundir)["status"] == "XFAIL" and "hoprnet#8392" in last_verdict(rundir)["msg"]
    c.xfail("hoprnet#8392", False, "defect absent")
    assert last_verdict(rundir)["status"] == "XPASS"
    with pytest.raises(pytest.fail.Exception):
        c.conclude()                     # the gate is green under a tag: the run must go red until the tag is removed
    d = Checks(rundir, "T98", "diagnostic")
    d.xfail("hoprnet#8392", False, "defect absent")
    d.conclude()                         # a diagnostic's XPASS is recorded only


def test_assert_min_max_name_the_knob(rundir):
    c = Checks(rundir, "T99", "gate")
    assert c.assert_min("download median", 9.5, "Mbit/s", "DOWN_MIN_MBIT", 7)
    assert "DOWN_MIN_MBIT=7" in last_verdict(rundir)["msg"]
    assert not c.assert_max("p95", 2000, "ms", "P95_MAX", 1500)
    assert last_verdict(rundir)["status"] == "FAIL"


def test_skip_records_and_raises(rundir):
    c = Checks(rundir, "T99", "gate")
    with pytest.raises(pytest.skip.Exception):
        c.skip("no second client")
    assert last_verdict(rundir)["status"] == "SKIP"


def test_knob_precedence(tmp_path):
    env = {"T23_DUR": "150", "SUITE_OUT_DIR": str(tmp_path)}
    cfg = Config(env, very_fast=True, knobs={"T24_DUR": "33"})
    spec = dict(DUR=q(3600, 600), INTERVAL=300)
    assert cfg.knobs("T23", spec).DUR == 150          # environment beats --very-fast (60)
    assert cfg.knobs("T23", spec).INTERVAL == 30      # --very-fast override
    assert cfg.knobs("T24", dict(DUR=q(900, 240))).DUR == 33   # --knob beats everything
    assert cfg.knobs("T05", dict(PHASE_S=q(30, 15))).PHASE_S == 8
    assert Config({}, fast=True).knobs("T05", dict(PHASE_S=q(30, 15))).PHASE_S == 15
    assert Config({}).knobs("T05", dict(PHASE_S=q(30, 15))).PHASE_S == 30
    assert cfg.bytes == 2000000 and Config({"BYTES": "7"}, very_fast=True).bytes == 7


def test_knob_types_follow_the_default():
    cfg = Config({"T06_ECHO_RATE": "2", "T06_LABEL": "x", "T06_ON": "0"})
    k = cfg.knobs("T06", dict(ECHO_RATE=1.5, LABEL="t06", ON=1))
    assert k.ECHO_RATE == 2.0 and k.LABEL == "x" and k.ON == 0
    assert cfg.knobs("T09", dict(RUNGS="0 25 50")).numbers("RUNGS") == [0, 25, 50]


def test_log_error_counters():
    lines = ["2026 WARN gnosis_vpn: failed to reassemble frame 12",
             "2026 DEBUG routing_actor: should_reconnect false failed to reassemble",     # DEBUG never counts
             "2026 DEBUG TunnelPingResult: Error(Ping timed out)",                        # counted regardless of level
             "2026 ERROR watchdog: exceeded max failures - reconnecting",
             "2026 WARN received worker response: failed to reassemble"]                 # excluded line
    c = count_log_errors(lines)
    assert c["reassembly_failed"] == 1 and c["ping_timeouts"] == 1 and c["reconnects"] == 1 and c["warn_error_lines"] == 2


def test_telemetry_sum_over_labels():
    txt = 'hopr_packet_rejected_count{reason="undecodable"} 3\nhopr_packet_rejected_count{reason="other"} 4\nother 9\n'
    assert telemetry_sum(txt, "hopr_packet_rejected_count") == 7
    assert telemetry_sum(txt, 'hopr_packet_rejected_count{reason="undecodable"}') == 3
    assert telemetry_sum(txt, "absent") is None


def test_curl_result_parses_and_flags_completion():
    r = _curl_result("200 10000000 8.0 0.5", 10000000)
    assert r["complete"] and r["mbit"] == 10.0
    assert not _curl_result("000 0 0 0", 1)["complete"]
    assert not _curl_result("500 100 1 0.1", 100)["complete"]           # a full-size non-200 body is not a delivery
    assert not _curl_result("429 100 1 0.1", 100)["complete"]


def test_persec_stall_longest_zero_run(rundir, tmp_path):
    from suitelib.client import Client
    cfg = Config({"SUITE_OUT_DIR": str(tmp_path)})
    c = Client(cfg, rundir, name="dummy")
    (rundir / "persec-x.csv").write_text("1,100,0\n2,100,0\n3,100,0\n4,200,0\n5,200,0\n")
    assert c.persec_stall("x", "rx") == 2
    assert c.persec_stall("missing", "rx") == 0


def test_tomlcfg_set_and_keep(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("version = 6\n\n[a]\nx = 1\n\n[b]\ny = 2\n")
    tomlcfg.set_section(f, "[a]", "x = 9")
    tomlcfg.set_section(f, "[c]", "z = 3")
    t = f.read_text()
    assert "x = 9" in t and "[c]" in t and "y = 2" in t
    assert tomlcfg.section_value(f, "[a]", "x") == "9"
    g = tmp_path / "d.toml"
    g.write_text('[destinations.a]\naddress = "1"\n\n[destinations.b]\naddress = "2"\n\n[connection]\nk = 1\n')
    tomlcfg.keep_destinations(g, 1)
    t = g.read_text()
    assert "destinations.a" in t and "destinations.b" not in t and "[connection]" in t


def test_run_dir_in_client_uses_basename(tmp_path):
    r = RunDir(tmp_path / "abc")
    assert r.in_client == "/suite-out/abc"
    assert os.path.isdir(r / "logs") and os.path.isdir(r / "samples")


def test_t31_analyse_counts_a_trailing_slab(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "regression"))
    from test_t31_frame_forensics import analyse
    log = tmp_path / "client.log"
    lines = ["inbound datagram len=1500 failed=true"] * 3 + ["inbound datagram len=200 failed=false"] + \
            ["inbound datagram len=1500 failed=true"] * 2          # a run still open when the log ends
    log.write_text("\n".join(lines) + "\n")
    r = analyse(str(log))
    assert r["reads"] == 6 and r["failed"] == 5 and r["multi_slabs"] == 2 and r["max_slab"] == 4700


def test_t06_check_arm_counts_stalls_over_stall_max(rundir):
    """The stall count follows STALL_MAX over the probe's worst gaps: gaps of 6 s and 4 s are one stall at 5, two at 3, none at 7."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "regression"))
    from test_t06_realtime_udp import check_arm, expected_pkts
    from suitelib.config import Knobs
    report = {"loss_pct": 0.0, "sent": expected_pkts(1.5, 15, 1200), "worst_stalls": [[10.0, 6.0], [20.0, 4.0]],
              "rebinds": 0, "outage_total_s": 0, "delay_over_min_ms": {"p99": 30}, "duration_s": 15}
    errors = {"reconnects": 0, "ping_timeouts": 0}
    for stall_max, want in ((5, "1 stall(s) over STALL_MAX=5s"), (3, "2 stall(s) over STALL_MAX=3s")):
        c = Checks(rundir, "T06-realtime-udp", "gate")
        k = Knobs(dict(SIZE=1200, SAMPLE_MIN_PCT=80, LOSS_MAX=5, STALL_MAX=stall_max))
        check_arm(c, k, "echo", 1.5, 15, report, errors)
        v = last_verdict(rundir)
        assert v["status"] == "FAIL" and want in v["msg"] and "worst 6.0s" in v["msg"], v["msg"]
    c = Checks(rundir, "T06-realtime-udp", "gate")
    check_arm(c, Knobs(dict(SIZE=1200, SAMPLE_MIN_PCT=80, LOSS_MAX=5, STALL_MAX=7)), "echo", 1.5, 15, report, errors)
    assert last_verdict(rundir)["status"] == "PASS"
