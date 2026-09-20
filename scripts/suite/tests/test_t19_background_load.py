"""T19-background-load (diagnostic): client 1 downloads while client 2 is idle (control), then while client 2
fetches TRICKLE_KB every 2 s through the same exit. Degradation proportional to bytes is expected; a step change
is a finding."""
import time

from suitelib.client import ConnectFailed, connect_or_fail
from suitelib.target import curl_down

TEST = "T19-background-load"
KIND = "diagnostic"
KNOBS = dict(TRICKLES="10 100")


def test_background_load(cfg, client, client2, target, checks, knobs):
    k = knobs
    if client2 is None:
        checks.skip("second client not running (EXTRA_IDENTITIES=2 + just client2-start)")
    try:
        s2 = client2.connect(cfg.dest, 15)
    except ConnectFailed as e:
        return checks.failed(f"client 2 connect failed: {e}")
    with s2:
        s = connect_or_fail(checks, client, cfg.dest, 15, label="client 1")
        if not s:
            return
        with s:
            ctrl = curl_down(client, target.ip, cfg.bytes, cfg.cap)
            checks.row(arm="idle-neighbour", download=ctrl)
            res = [f"idle {ctrl['mbit']}"]
            for kb in k.words("TRICKLES"):
                client2.exec_bg(f"for i in $(seq 1 120); do curl -s -o /dev/null -m 10 {target.down_url(int(kb) * 1000)}; sleep 2; done")
                time.sleep(4)
                r = curl_down(client, target.ip, cfg.bytes, cfg.cap)
                client2.exec('pkill -f "seq 1 120"; pkill curl; true')
                checks.row(arm=f"trickle-{kb}kB", download=r)
                res.append(f"{kb}kB/2s {r['mbit']}")
    checks.passed("client-1 download Mbit/s with neighbour: " + "; ".join(res))
