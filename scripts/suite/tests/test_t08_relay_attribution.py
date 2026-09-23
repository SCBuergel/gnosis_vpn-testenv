"""T08-relay-attribution (gate): which relay carries the return traffic, and is the split what the topology implies?
One download of BYTES after a 10 s idle (floored to SURB_RAMP_WAIT) with the client at hopr_transport::path=debug;
counts 'resolved return path' lines per first-hop relay. The relay is the 20-byte chain address inside path=[...];
the destination= field is a 32-byte offchain key, and three wrong return-relay stories came from reading it as the
relay. The debug target writes ~50 MB/min, so it is scoped to this test.

Membership (gate): every return path's first hop is an Open outgoing channel peer of the exit, or the exit itself.
Split (gate only with SUITE_EQUAL_LATENCY=1, at least 2 relays and SPLIT_MIN_PATHS paths; recorded otherwise):
skew = 100 * (max - min) / sum <= SPLIT_TOL_PCT. 60 is not near-even on purpose: the split varies by release and
by session (about even on some reference runs, 86/14 on others; 9711/8254 passed, 10764/1822 failed), so the gate
fails an 80/20 split and beyond and is kept as a signal by decision. WARN when the log has no resolved-path lines.

Why: the split was ~85/15 on one version line and ~51/49 on another, a real behavioural change no throughput
number shows, and it stops a regression being blamed on 'the second relay' when 85 % of return traffic never
touched it. Candidates are exactly the exit's Open outgoing channels; a PendingToClose one drops out within a
minute, and one dead candidate fails about half of all health checks."""
import collections
import re

from suitelib.client import connect_or_fail
from suitelib.target import curl_down

TEST = "T08-relay-attribution"
KIND = "gate"
KNOBS = dict(SUITE_EQUAL_LATENCY=1, SPLIT_TOL_PCT=60, SPLIT_MIN_PATHS=1000)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
PATH = re.compile(r"path=validated path \[([^\]]*)\]")


def test_relay_attribution(cfg, client, cluster, target, checks, knobs):
    k = knobs
    s = connect_or_fail(checks, client, cfg.dest, 10)
    if not s:
        return
    with s:
        r = curl_down(client, target.ip, cfg.bytes, cfg.cap)
        by = collections.Counter()
        total = 0
        for line in client.log_lines(s.since):
            line = ANSI.sub("", line)
            if "resolved return path" not in line:
                continue
            m = PATH.search(line)
            if not m:
                continue
            hops = [h.strip() for h in m.group(1).split(",")]
            if len(hops) >= 2:
                by[hops[0].lower()] += 1
                total += 1
    split = {"total": total, "by_relay": dict(by)}
    if not cluster.available():
        checks.row(split=split, download=r)
        return checks.record(f"no localcluster: return-path split {dict(by)} recorded, membership not checked")
    me = cluster.address(0).lower()
    exits = [c["peerAddress"].lower() for c in cluster.open_outgoing(0)] + [me]
    checks.row(split=split, exit_open_outgoing=exits, download=r, equal_latency=k.SUITE_EQUAL_LATENCY)
    if total == 0:
        checks.warn("no 'resolved return path' lines (is hopr_transport::path=debug in CLIENT_LOG_LEVEL?)")
        return
    # part 1 - membership (always a gate)
    outside = [x for x in by if x not in exits]
    if not outside:
        checks.passed(f"membership: all {total} return paths start at an open outgoing channel of the exit - {dict(by)}")
    else:
        checks.failed(f"membership: a return relay is outside the exit's open channel set: {dict(by)} vs {exits}")
    # part 2 - split, scored only when the relays are at equal latency
    counts = sorted((n for a, n in by.items() if a != me), reverse=True)
    skew = round(100 * (counts[0] - counts[-1]) / max(sum(counts), 1), 1) if len(counts) > 1 else None
    if len(counts) < 2 or skew is None:
        checks.record(f"split: only {len(counts)} return relay(s) in play, distribution not meaningful")
    elif sum(counts) < k.SPLIT_MIN_PATHS:
        checks.record(f"split: only {sum(counts)} return paths (floor SPLIT_MIN_PATHS={k.SPLIT_MIN_PATHS}), skew {skew}% recorded, not gated")
    elif k.SUITE_EQUAL_LATENCY == 1:
        if skew <= k.SPLIT_TOL_PCT:
            checks.passed(f"split on equal-latency relays: skew {skew}% <= {k.SPLIT_TOL_PCT}% {counts}")
        else:
            checks.failed(f"split on equal-latency relays skewed {skew}% > {k.SPLIT_TOL_PCT}% {counts} - planner behaviour change")
    else:
        checks.record(f"split under unequal latency (diagnostic): skew {skew}% {counts}")
