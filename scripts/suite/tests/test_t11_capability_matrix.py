"""T11-capability-matrix (gate): does the tunnel behave as expected under each [connection.wg] capability set the
client can request? Cells: segmentation+no_delay (the default; exit shaper expected), segmentation alone (expected),
and the default plus no_rate_control (not expected). Each cell restarts the client with the set, connects, runs one
CALL_S call and polls the exit's /metrics POLL_N times at 2 s for per-session hopr_surb_balancer_* series, which
exist exactly while the exit shapes a session (the 'spawning exit SURB balancer' line is not logged at the default
level). Decapsulation errors come from the client log.

Pass iff, per cell, decap_error = 0 and the per-session balancer series count rose exactly when a shaper is expected.
Call loss and download rate are recorded.

Why: the exit's datagram mode on release/4.0 keys off the client's no_delay flag, so a client that drops it gets
frames cut at frame_size again; no_rate_control measured inert for throughput but removes the shaper, so a fresh
session then drops egress when SURBs run out instead of shaping. Config-only per cell and the only test that
catches a capability-dependency regression on either side."""
import re
import shutil
import time

from suitelib import tomlcfg
from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.target import curl_down

TEST = "T11-capability-matrix"
KIND = "gate"
KNOBS = dict(CALL_S=q(60, 30), POLL_N=10)
SHAPER = re.compile(r"^hopr_surb_balancer_[a-z_]+\{[^}]*session_id=", re.M)
CELLS = [("default", '["segmentation", "no_delay"]', 1),
         ("segmentation-only", '["segmentation"]', 1),
         ("no-rate-control", '["segmentation", "no_delay", "no_rate_control"]', 0)]


def test_capability_matrix(cfg, run, client, live_cluster, target, checks, knobs):
    cluster = live_cluster
    k = knobs
    cfg_file = cfg.config_dir / "client.toml"
    orig = run / "client.toml.t11.orig"
    shutil.copy(cfg_file, orig)
    wg_target = tomlcfg.section_value(orig, "[connection.wg]", "target") or "127.0.0.1:51821"

    def shaper_series():
        return len(SHAPER.findall(cluster.metrics(0)))

    try:
        for name, caps, expect in CELLS:
            tomlcfg.set_section(cfg_file, "[connection.wg]", f"capabilities = {caps}", f'target = "{wg_target}"')
            if not client.restart():
                checks.failed(f"{name}: client restart failed")
                continue
            before = shaper_series()
            s = connect_or_fail(checks, client, cfg.dest, 0, label=name)
            if not s:
                continue
            with s:
                during = 0
                for _ in range(k.POLL_N):
                    during = max(during, shaper_series())
                    time.sleep(2)
                r = curl_down(client, target.ip, cfg.bytes, cfg.cap)
                client.probe("relprobe", f"t11-{name}", timeout=k.CALL_S + 90, host=target.ip, port=target.echo_port, rate_mbit=1.5,
                             duration=k.CALL_S, size=1200, iface=s.iface)
                e = s.errors()
            call = run.read_json(f"t11-{name}.json", {})
            shaped = int(during > before)
            checks.row(cell=name, caps=caps, download=r, call=call, errors=e, shaper_series_before=before, shaper_series_peak=during,
                       shaped=shaped, expect_shaper=expect)
            msg = (f"{name} {caps}: decap {e['decap_error']}, call loss {call.get('loss_pct')}%, exit shaper series {before}->{during} "
                   f"(shaped={shaped}, expected {expect}), down {r['mbit']} Mbit/s")
            checks.verdict(e["decap_error"] == 0 and shaped == expect, msg)
    finally:
        shutil.copy(orig, cfg_file)
        if not client.restart():
            checks.failed("restore: client restart on the original config failed; later tests start from a stopped client")
