"""T08-relay-attribution (gate): parses the client's path-planner lines during a download and counts resolved
return paths per first-hop relay (the 20-byte address inside path=[...], not the destination field).
membership (gate): every return path's first hop is an open outgoing channel of the exit.
split (gate only when the relays carry equal injected latency, diagnostic otherwise): SPLIT_TOL_PCT is 60 %,
not near-even, because the split legitimately varies by version (51/49 to 85/15); it fails only when one relay
carries under ~20 % of return paths."""
import collections
import re

from suitelib.client import connect_or_fail
from suitelib.target import curl_down

TEST = "T08-relay-attribution"
KIND = "gate"
KNOBS = dict(SUITE_EQUAL_LATENCY=1, SPLIT_TOL_PCT=60)
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
    elif k.SUITE_EQUAL_LATENCY == 1:
        if skew <= k.SPLIT_TOL_PCT:
            checks.passed(f"split on equal-latency relays: skew {skew}% <= {k.SPLIT_TOL_PCT}% {counts}")
        else:
            checks.failed(f"split on equal-latency relays skewed {skew}% > {k.SPLIT_TOL_PCT}% {counts} - planner behaviour change")
    else:
        checks.record(f"split under unequal latency (diagnostic): skew {skew}% {counts}")
