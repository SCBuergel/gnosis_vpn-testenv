"""T31-frame-forensics (runbook): when the client rejects inbound traffic, what exactly is arriving? One cold
connect and one download of BYTES on a client built with the inbound-read instrumentation (open extension 3),
which logs 'inbound datagram ... len=N'; SKIP when the running image does not emit those lines. Reduces the log to
a read-length histogram, the fraction failing decapsulation, and a slab analysis grouping reads into runs (a
sequence of frame_size reads ended by a short one). PASS iff no packed slab is found, i.e. no run of inbound reads
summing to more than 1500 B before a short read; FAIL names the slab count and the failed-read fraction.

Why: the only technique that produced proof rather than inference about the datagram-boundary defect. Healthy: 600
reads, 0 failed, max 1452 B. Broken: 256 reads, 95 % failed, 201 of exactly 1500 B and all 201 failing, each packed
run a whole-datagram slab of 14-16 kB under the 16 384-byte bridge buffer."""
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
