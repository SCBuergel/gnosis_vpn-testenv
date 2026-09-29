"""T34-single-relay-scaling (gate): how many concurrent clients can one relay carry? Rungs of LADDER="1 2 3 4 5":
n clients, each with its own exit, all through the same single relay in both directions (client k -> relay ->
exit k, and back over the relay), download DOWN_BYTES=25 MB at once, then upload UP_BYTES at once. Read against
T33-relay-baseline, where the same number of clients has as many relays: where this ladder falls below that one,
the single relay is the limit, not the host.

The rest is T33-relay-baseline's procedure: the channel graph is held against the topology before every rung
(every client and every exit exactly one Open channel, to the one relay), IDLE_S=10 after connecting, PAUSE_S=10
between phases and rungs, FAIL only when a transfer does not complete within CAP=180 s, rates recorded not scored,
the relay must have forwarded at least one packet per PKT_BYTES_MAX downloaded bytes (with one relay the share
check of T33 is 100 % by construction and only recorded), relay and host CPU recorded next to the rates.

Setup: `just relay-topology shared 5` (CLUSTER_SIZE=6 with --channel-management none: one relay, five exits, five
clients, five VPN servers), then `just test t34`. SKIP on any other stack."""
from suitelib import relaybench

TEST = "T34-single-relay-scaling"
KIND = "gate"
GROUP = "relayscale"
KNOBS = dict(relaybench.KNOBS)


def TIMEOUT(knobs):
    return relaybench.timeout(knobs)


def test_single_relay_scaling(cfg, run, cluster, target, checks, knobs):
    relaybench.run_ladder(cfg, run, cluster, target, checks, knobs, "shared")
