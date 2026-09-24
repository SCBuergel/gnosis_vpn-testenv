"""T29-destination-sweep (runbook, production): which exits accept a tunnel right now, and at what speed? For every
destination the client reports: connect, idle SURB_RAMP_WAIT (25 s; the 5 s asked for is floored to it on purpose,
a shorter idle starves the first transfer's return path), in-tunnel ping, one download of BYTES, disconnect;
CYCLES rounds; one row per destination and cycle, so a sweep costs about destinations x CYCLES x (connect + 25 s
+ the download). PASS per destination and cycle iff the connect succeeds; RTT and rate are recorded.

Why: a 40-run cycle over 8 exits found one that connected but carried ~0 and one that stayed down for hours after
a ladder; a page-load campaign found the same two."""
from suitelib.client import ConnectFailed
from suitelib.config import q
from suitelib.target import curl_down, ping_avg

TEST = "T29-destination-sweep"
KIND = "runbook"
KNOBS = dict(CYCLES=q(3, 1))


def test_destination_sweep(cfg, client, target, checks, knobs):
    for c in range(1, knobs.CYCLES + 1):
        for d in client.destinations():
            try:
                s = client.connect(d, 5)
            except ConnectFailed:
                checks.row(cycle=c, dest=d, connect=False)
                checks.failed(f"{d} cycle {c}: connect failed")
                continue
            with s:
                rtt = ping_avg(client, target.ip, count=3, interval=0.3, wait=3)
                r = curl_down(client, target.ip, cfg.bytes, cfg.cap)
            checks.row(cycle=c, dest=d, connect=True, connect_ms=s.connect_ms, rtt_ms=rtt, download=r)
            checks.passed(f"{d} cycle {c}: connect {s.connect_ms} ms, rtt {rtt} ms, down {r['mbit']} Mbit/s")
