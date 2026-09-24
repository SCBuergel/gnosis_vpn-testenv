"""T19-background-load (diagnostic): how much does any other activity on the same exit cost the client doing real
work? Client 1 downloads BYTES while client 2 is idle (control), then while client 2 fetches TRICKLES kB every 2 s
through the same exit, one arm per size; the trickle loop records its pid and is killed by it (never by a
pkill -f pattern, which matches the killer's own command line). SKIP unless CLIENT2 runs. Degradation proportional to bytes is expected;
a trickle arm that falls below STEP_FRAC of the idle control is the step change this test exists for. WARN (a
diagnostic never fails the run) when the idle control does not complete or any trickle arm is incomplete or below
STEP_FRAC x the idle rate; client 1's rate per arm and its ratio to idle are always recorded.

Why: on four 2-slot exits an idle neighbour let the download finish at 4.4-6.6 Mbit/s, a 10 kB/2 s trickle cut it
to 1.4-2.0 and it hit the cap, and ten times the bytes changed nothing further; uploads were untouched. The scarce
resource is return-path capacity spent per request, not per byte. It is the most realistic multi-user scenario
there is and far cheaper than T22."""
import time

from suitelib.client import ConnectFailed, connect_or_fail
from suitelib.target import curl_down

TEST = "T19-background-load"
KIND = "diagnostic"
KNOBS = dict(TRICKLES="10 100", STEP_FRAC=0.5)


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
            res, steps = [f"idle {ctrl['mbit']}"], []
            for kb in k.words("TRICKLES"):
                client2.exec_bg(f"echo $$ > /tmp/t19-trickle.pid; for i in $(seq 1 120); do curl -s -o /dev/null -m 10 "
                                f"{target.down_url(int(kb) * 1000)}; sleep 2; done")
                time.sleep(4)
                r = curl_down(client, target.ip, cfg.bytes, cfg.cap)
                # by pid, then the in-flight curl by exact name: a pkill -f pattern would match this shell's own command line;
                # the loop's current `sleep 2` is left to expire on its own
                client2.exec('kill "$(cat /tmp/t19-trickle.pid 2>/dev/null)" 2>/dev/null; pkill -x curl; rm -f /tmp/t19-trickle.pid; true')
                ratio = round(r["mbit"] / ctrl["mbit"], 2) if ctrl["mbit"] > 0 else None
                checks.row(arm=f"trickle-{kb}kB", download=r, ratio_to_idle=ratio)
                res.append(f"{kb}kB/2s {r['mbit']} (x{ratio} of idle)")
                if not r["complete"] or (ratio is not None and ratio < k.STEP_FRAC):
                    steps.append(f"{kb}kB/2s: {r['mbit']} Mbit/s is x{ratio} of the idle control{'' if r['complete'] else ', incomplete'}")
    msg = "client-1 download Mbit/s with neighbour: " + "; ".join(res)
    if not ctrl["complete"]:
        checks.failed(f"idle control did not complete; {msg}")
    elif steps:
        checks.failed(f"step change under a trickling neighbour (below STEP_FRAC={k.STEP_FRAC} of idle): " + "; ".join(steps) + f"; {msg}")
    else:
        checks.passed(msg)
