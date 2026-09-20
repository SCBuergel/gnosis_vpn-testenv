"""T31-frame-forensics (runbook): read-length histogram and slab analysis of every inbound datagram during a cold
start. Needs a client image built with the inbound-read instrumentation (catalogue extension 12) that logs
'inbound datagram ... len=N'; skips when the running image does not emit those lines."""
import collections
import re

from suitelib.client import connect_or_fail
from suitelib.target import curl_down

TEST = "T31-frame-forensics"
KIND = "runbook"
KNOBS = {}


def analyse(path):
    reads, fails = [], 0
    for line in open(path, errors="replace"):
        if "inbound datagram" not in line:
            continue
        m = re.search(r"len=(\d+)", line)
        if not m:
            continue
        n, f = int(m.group(1)), "failed=true" in line
        reads.append((n, f))
        fails += f
    hist = collections.Counter(n for n, _ in reads)
    slabs, cur = [], 0
    for n, _ in reads:
        cur += n
        if n < 1500:
            slabs.append(cur)
            cur = 0
    multi = [s for s in slabs if s > 1500]
    return {"reads": len(reads), "failed": fails, "max_read": max(hist) if hist else 0, "top": hist.most_common(5),
            "multi_slabs": len(multi), "max_slab": max(multi) if multi else 0}


def test_frame_forensics(cfg, client, target, checks, knobs):
    s = connect_or_fail(checks, client, cfg.dest, 0)
    if not s:
        return
    with s:
        r = curl_down(client, target.ip, cfg.bytes, cfg.cap)
        n = client.count_log(s.since, "inbound datagram")
        path = s.save_log("t31")
    if n == 0:
        checks.skip("client image has no inbound-read instrumentation (0 'inbound datagram' lines)")
    an = analyse(path)
    checks.row(analysis=an, download=r)
    if an["multi_slabs"] == 0:
        checks.passed(f"{an['reads']} reads, {an['failed']} failed, max read {an['max_read']} B, no packed slabs")
    else:
        checks.failed(f"{an['multi_slabs']} packed slabs (max {an['max_slab']} B), {an['failed']}/{an['reads']} reads failed")
