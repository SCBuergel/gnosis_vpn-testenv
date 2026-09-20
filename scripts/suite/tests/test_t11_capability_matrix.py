"""T11-capability-matrix (gate): cells over [connection.wg] capabilities: the default segmentation+no_delay,
segmentation only, and the default plus no_rate_control. The exit's datagram mode keys off the client's no_delay
flag and its egress shaper exists exactly when no_rate_control is absent. The shaper is detected from the exit's
per-session SURB-balancer metrics (hopr_surb_balancer_*{session_id=...} appear exactly while it shapes a
session); the "spawning exit SURB balancer" debug line is not emitted at the default node log level."""
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


def test_capability_matrix(cfg, run, client, cluster, target, checks, knobs):
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
        client.restart()
