"""T35-single-exit-scaling (gate): how many concurrent clients can one exit carry? Rungs of LADDER="1 2 3 4 5": n
clients, each with a relay of its own, all to the same single exit, download DOWN_BYTES=100 MB at once, then upload
UP_BYTES=100 MB at once. The twin of T34-single-relay-scaling (one relay, an exit per client): same ladder, same
settings, same report, with the exit under test instead of the relay.

The topology (`single-exit`, suitelib/relaytopo.py): client k holds one channel, to relay k, so its forward path is
client -> relay k -> exit; the exit holds one channel to each relay, so it may send a client's return traffic over any
relay (the exit picks among its channels; with one exit this cannot be pinned). Every client has its own VPN server
on the exit's machine. The channel graph is held against that before every rung.

Procedure and scoring as T34-single-relay-scaling (suitelib/relaybench.py): IDLE_S=10 after connecting, one common
start per phase (START_LEAD_S=5, start skew reported), PAUSE_S=10 between phases and rungs, FAIL only when a transfer
does not complete within CAP=300 s, rates over the overlap in which every client was transferring. Reported per rung:
the exit's machine CPU (% of all its cores; it also runs the VPN servers and the traffic target) and its hoprd
process's CPU (% of one core), the other roles' machines, the versions and one line of machine specs per role. The
relays together must have forwarded one packet per PKT_BYTES_MAX downloaded bytes; which relay carried a return path
is recorded, not scored.

Before 2026-09-30 this configuration ran as T22-concurrent-clients against a single-exit stack; T22 keeps its own
standard stack (two relays and one exit in a full mesh). Setup: `just multihost-up HOSTS single-exit N` (or
`just relay-topology single-exit N` on one machine), then `just multihost-test t35` / `just test t35`. SKIP on any
other stack."""
from suitelib import relaybench

TEST = "T35-single-exit-scaling"
KIND = "gate"
GROUP = "relayscale"
KNOBS = dict(relaybench.SCALING_KNOBS)


def TIMEOUT(knobs):
    return relaybench.timeout(knobs)


def test_single_exit_scaling(cfg, run, cluster, target, checks, knobs):
    relaybench.run_ladder(cfg, run, cluster, target, checks, knobs, "single-exit", under_test="exit")
