"""T12-balancer-sweep (gate): how do the client's SURB balancer profiles move connect time, cold-start behaviour and
steady-state throughput, and where does the sweep collapse? Cells over [connection.surb_balancing.main]
max_surb_upstream in UPSTREAMS plus one cell with the raised ping tier (10 MB / 16 Mb/s); each cell restarts the
client with the config, connects (recording the time), runs one cold and one warm download of BYTES, and reads the
exit's session target. PASSES passes, even ones in reverse order. Then the masking cell: a cold download on the
default ping tier against one on the raised tier.

Pass iff the best warm download > 0 and the worst warm download across cells >= 0.3 x the best; and the masking
cell's cold download on the default ping tier completes within CAP (n = 1), whatever the raised tier did (both are
named, so a raised tier that passes shows the tuning masks a default-config defect). The masking cell is n = 1 and
the collapse is intermittent, so its PASS does not distinguish a fixed ramp from a lucky probe; check its
sensitivity before trusting it.

Why: 96 Mb/s collapsed downloads under a frame-discard storm while 48 was at baseline, and the raised ping tier was
exactly the tuned config that hid the 0.96.1 ramp bug for a day. The axis has a known cliff and a known mask."""
import re
import shutil
import time

from suitelib import tomlcfg
from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.target import curl_down

TEST = "T12-balancer-sweep"
KIND = "gate"
GROUP = "config"
KNOBS = dict(UPSTREAMS=q("12 16 48 96", "16 96"), PASSES=q(2, 1))
TIMEOUT = lambda k: (k.PASSES * (len(k.words("UPSTREAMS")) + 1) + 2) * 1500   # seconds; the harness fails the test past this
RAISED_PING = ("[connection.surb_balancing.ping]", "enabled = true", 'buffer = "10 MB"', 'max_surb_upstream = "16 Mb/s"')


def test_balancer_sweep(cfg, run, client, cluster, target, checks, knobs):
    k = knobs
    cfg_file = cfg.config_dir / "client.toml"
    orig = run / "client.toml.t12.orig"
    shutil.copy(cfg_file, orig)
    cells = [f"main:{u}" for u in k.words("UPSTREAMS")] + ["ping:10MB"]
    best, worst = 0.0, 999999.0

    def exit_target():
        try:
            lines = [l for l in open(cluster.log_file(0), errors="replace") if "spawning exit SURB balancer" in l]
        except OSError:
            return ""
        m = re.search(r"target_surb_buffer_size: [0-9]+", lines[-1]) if lines else None
        return m.group(0) if m else ""

    def run_cell(c):
        nonlocal best, worst
        shutil.copy(orig, cfg_file)
        if c.startswith("main:"):
            tomlcfg.set_section(cfg_file, "[connection.surb_balancing.main]", "enabled = true", 'buffer = "10 MB"',
                                f'max_surb_upstream = "{c[5:]} Mb/s"')
        else:
            tomlcfg.set_section(cfg_file, *RAISED_PING)
        if not client.restart():
            return checks.failed(f"{c}: client restart failed")
        s = connect_or_fail(checks, client, cfg.dest, 0, label=c)
        if not s:
            return
        with s:
            cold = curl_down(client, target.ip, cfg.bytes, cfg.cap)
            time.sleep(20)
            warm = curl_down(client, target.ip, cfg.bytes, cfg.cap)
            e = s.errors()
            tgt = exit_target()
        checks.row(cell=c, connect_ms=s.connect_ms, cold=cold, warm=warm, errors=e, exit_target=tgt)
        best, worst = max(best, warm["mbit"]), min(worst, warm["mbit"])
        checks.log(f"{c}: connect {s.connect_ms} ms, cold {cold['mbit']}, warm {warm['mbit']} Mbit/s, discards {e['frame_discarded']}, exit {tgt}")

    try:
        for p in range(1, k.PASSES + 1):
            for c in (cells if p % 2 else reversed(cells)):
                run_cell(c)
        if best > 0 and worst >= 0.3 * best:
            checks.passed(f"warm download across cells: worst {worst}, best {best} Mbit/s (no collapse)")
        else:
            checks.failed(f"a cell collapsed: worst {worst} vs best {best} Mbit/s")
        # masking cell
        shutil.copy(orig, cfg_file)
        if not client.restart():
            checks.failed("masking: client restart on the default config failed")
        d_def = {"mbit": 0, "complete": False}
        s = connect_or_fail(checks, client, cfg.dest, 0, label="masking-default")
        if s:
            with s:
                d_def = curl_down(client, target.ip, cfg.bytes, cfg.cap)
        tomlcfg.set_section(cfg_file, *RAISED_PING)
        if not client.restart():
            checks.failed("masking: client restart on the raised ping tier failed")
        d_tun = {"mbit": 0, "complete": False}
        s = connect_or_fail(checks, client, cfg.dest, 0, label="masking-raised")
        if s:
            with s:
                d_tun = curl_down(client, target.ip, cfg.bytes, cfg.cap)
        checks.row(cell="masking", default_ping_tier=d_def, raised_ping_tier=d_tun)
        if d_def["complete"]:
            checks.passed(f"masking: cold start completes on the DEFAULT ping tier ({d_def['mbit']} Mbit/s; raised tier {d_tun['mbit']})")
        else:
            checks.failed(f"masking: cold start fails on defaults ({d_def['mbit']} Mbit/s) but the raised ping tier gives {d_tun['mbit']} "
                          f"- tuning is hiding a default-config defect")
    finally:
        shutil.copy(orig, cfg_file)
        if not client.restart():
            checks.failed("restore: client restart on the original config failed; later tests start from a stopped client")
