"""T21-passive-observer (diagnostic): the second client never connects; it polls destination health for DUR s
alongside client 1. Pass: both clients report the same Ready count in at least 80 % of samples."""
import time

from suitelib.config import q

TEST = "T21-passive-observer"
KIND = "diagnostic"
KNOBS = dict(DUR=q(300, 60))
TIMEOUT = lambda k: k.DUR + 300   # seconds; the harness fails the test past this


def test_passive_observer(cfg, run, client, client2, checks, knobs):
    k = knobs
    if client2 is None:
        checks.skip("second client not running (EXTRA_IDENTITIES=2 + just client2-start)")
    t0 = time.time()
    samples = []
    with open(run / "t21-observer.csv", "w") as f:
        while time.time() - t0 < k.DUR:
            a = client.status_text().count("Route health: Ready")
            b = client2.status_text().count("Route health: Ready")
            f.write(f"{int(time.time())},{a},{b}\n")
            samples.append((a, b))
            time.sleep(15)
    dis = sum(1 for a, b in samples if a != b)
    n = len(samples)
    checks.row(disagreements=dis, samples=n)
    if dis <= n // 5:
        checks.passed(f"active and passive client agreed on Ready destinations in {n - dis}/{n} samples")
    else:
        checks.failed(f"views diverged in {dis}/{n} samples (local client state, not the network)")
