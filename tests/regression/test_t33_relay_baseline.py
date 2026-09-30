"""T33-relay-baseline (gate): what does one relay carry when every client has a relay and an exit of its own, as
clients, relays and exits grow together? Rungs of LADDER="1 2 3 4 5": n clients, each pinned to its own relay and
its own exit (client k -> relay k -> exit k, and the return path back over relay k), download DOWN_BYTES=25 MB at
once, then upload UP_BYTES at once. On one host the relays share the machine, so the ladder calibrates what the
host itself allows; T34-single-relay-scaling runs the same ladder through one relay and is read against this one.

Before every rung the chain's channel graph is held against the topology: every client and every exit has exactly
one Open outgoing channel, to its assigned relay; anything else fails the rung before it runs. The clients wait
IDLE_S=10 after connecting (below the suite's SURB_RAMP_WAIT floor on purpose: the request is a 10 s wait, and the
first seconds of each transfer still ride the client's SURB ramp), and PAUSE_S=10 separates the download, the
upload and the rungs. Only one threshold: a rung FAILs if any transfer does not complete within CAP=180 s (25 MB in
180 s is 1.1 Mbit/s); the rates are recorded, not scored, until the baseline is calibrated. Also a check that the
download crossed the rung's relays: they forwarded at least one packet per PKT_BYTES_MAX downloaded bytes, and
ATTRIB_MIN_PCT of all relayed packets (RELAY_METRIC delta) were on the rung's own relays.

Setup: `just relay-topology paired 5` (takes the stack down and builds the topology: CLUSTER_SIZE=10 with
--channel-management none, five clients and five VPN servers), then `just test t33`. Results: rows and
T33-relay-baseline.md (one line per rung) in the run directory. SKIP on any other stack."""
from suitelib import relaybench

TEST = "T33-relay-baseline"
KIND = "gate"
GROUP = "relayscale"
KNOBS = dict(relaybench.KNOBS)


def TIMEOUT(knobs):
    return relaybench.timeout(knobs)


def test_relay_baseline(cfg, run, cluster, target, checks, knobs):
    relaybench.run_ladder(cfg, run, cluster, target, checks, knobs, "paired", under_test="relay")
