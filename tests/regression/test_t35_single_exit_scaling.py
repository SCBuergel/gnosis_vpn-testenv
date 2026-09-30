"""T35-single-exit-scaling (gate): how many concurrent clients can one exit carry? Rungs of LADDER="1 2 3 4 5": n
clients, all to the same single exit, download DOWN_BYTES=100 MB at once, then upload UP_BYTES=100 MB at once. The twin
of T34-single-relay-scaling (one relay, an exit per client): same ladder, settings and report, with the exit under test
instead of the relay.

What it measures, exactly: one exit behind a POOL of relays, not a relay per client. The topology (`single-exit`,
suitelib/relaytopo.py) gives client k one channel, to relay k, so its forward path (its upload) is pinned to relay k.
The exit holds one channel to every relay and picks among them for return paths, so every client's download comes back
over all the relays: in the 2026-09-30 run each of the ten relays carried 9-10 % of the return traffic at every rung,
also with one client. The relays are therefore never the limit, and T35's rates are not those of "a relay per client".
Pinning the return path would need an exit with only the rung's relays as channels, i.e. a fresh stack per rung (closing
channels inside a run is what AGENTS.md forbids). Read T35 as the exit's capacity behind enough relays, and check the
exit's machine CPU: if it is far from saturated, the top rung is a lower bound on what one exit carries.

Procedure and scoring as T34-single-relay-scaling (suitelib/relaybench.py): the channel graph is held against the
topology before every rung, IDLE_S=10 after connecting, one common start per phase (START_LEAD_S=5, start skew
reported), PAUSE_S=10 between phases and rungs, FAIL only when a transfer does not complete within CAP=300 s, rates over
the overlap in which every client was transferring. Reported per rung: the exit's machine CPU (% of all its cores; it
also runs a VPN server per client and the traffic target) and its hoprd process's CPU (% of one core), the other roles'
machines, the versions and one line of machine specs per role. The relays together must have forwarded one packet per
PKT_BYTES_MAX downloaded bytes; which relay carried a return path is recorded, not scored.

T22-concurrent-clients keeps its own standard stack (two relays and one exit in a full mesh). Setup: `just
multihost-up HOSTS single-exit N` (or `just relay-topology single-exit N` on one machine), then `just multihost-test
t35` / `just test t35`. SKIP on any other stack."""
from suitelib import relaybench

TEST = "T35-single-exit-scaling"
KIND = "gate"
GROUP = "relayscale"
KNOBS = dict(relaybench.SCALING_KNOBS)


def TIMEOUT(knobs):
    return relaybench.timeout(knobs)


def test_single_exit_scaling(cfg, run, cluster, target, checks, knobs):
    relaybench.run_ladder(cfg, run, cluster, target, checks, knobs, "single-exit", under_test="exit")
