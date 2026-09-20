"""T29-destination-sweep (runbook): for every destination the client reports: connect, in-tunnel ping, one
download, disconnect; CYCLES rounds. Pass: every destination connects in every cycle."""
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
