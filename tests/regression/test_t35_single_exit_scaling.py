"""T35-single-exit-scaling (gate): how many concurrent clients can one exit carry? Rungs of LADDER="1 2 3 4 5": n
clients, all to the same single exit, download DOWN_BYTES=100 MB at once, then upload UP_BYTES=100 MB at once. The twin
of T34-single-relay-scaling (one relay, an exit per client): same ladder, settings and report, with the exit under test
instead of the relay.

What it measures, exactly: one exit (one hoprd exit node with its one VPN server, as in the field) behind a POOL of
relays, not a relay per client. The topology (`single-exit`, suitelib/relaytopo.py) gives client k one channel, to
relay k, so its forward path (its upload) is pinned to relay k. The exit holds one channel to every relay, and a
client builds its return paths over any of them, so every client's download comes back over all the relays: in the
2026-09-30 run each of the ten relays carried 9-10 % of the return traffic at every rung, also with one client. The
relays are therefore never the limit, and T35's rates are not those of "a relay per client". Pinning the return path
would need an exit with only the rung's relays as channels, i.e. a fresh stack per rung (closing channels inside a run
is what AGENTS.md forbids).

How to read it. The aggregate is what the exit carried, not what it can carry: each client tops out by itself (9-12
Mbit/s on this client build; in the 2026-10-01 run a client's own 4-vCPU relay and its own machine stood at 60-66 %
during its upload, also alone), so n clients offer about n times that and the ladder shows whether the exit still
passes it on. Signs that the exit is at its limit: the per-client rate falls; the aggregate buckets spread (the
minimum drops while the maximum holds: the path collapses and recovers); the exit machine's UDP error counters start
to move during the UPLOADS (on downloads they move at every rung, also with one client, so there they say nothing);
the exit process stops gaining CPU. Machine CPU alone does not tell: a hoprd node levels off near 60 % of its machine
(T34's relay: 9.5 of 16 cores), and the 2026-09-30 run read "not saturated" from 40-55 % while its uploads were already
collapsing in step from 8 clients up. Before blaming the exit, look at the relay machines: in the 2026-10-01 run the
ten 4-vCPU relays stood at 59-62 % in the top download rungs, at that same level, so those rungs do not tell the exit
from the relay pool. The 2026-09-30 run also gave every client a VPN server of its own on the exit's machine, ten
where the field has one; the topology now has one (the numbers did not move).

Procedure and scoring as T34-single-relay-scaling (suitelib/relaybench.py): the channel graph is held against the
topology before every rung, IDLE_S=10 after connecting, one common start per phase (START_LEAD_S=5), PAUSE_S=10 between
phases and rungs, rates over the overlap in which every client was transferring, upload bytes counted when the
target's TCP acknowledged them. FAIL when a transfer does not complete within CAP=300 s, when a complete transfer's
bytes are not on its client's tunnel interface counters, when the relays together forwarded less than one packet per
PKT_BYTES_MAX transferred bytes in either phase, or (exit on its own machine) when the exit machine's wire sent less
than the downloaded bytes or received less than the uploaded ones. Reported per rung: the exit's machine CPU (% of
all its cores; the machine also runs the VPN server and the traffic target) and its hoprd process's CPU (% of one
core) over the overlap window, the aggregate per BUCKET_S=5 s (min / median / max), the longest stall, the frames the
clients discarded, the exit machine's UDP errors and wire bytes, the exit's own packet counters, the other roles'
machines, the versions and one line of machine specs per role. Which relay carried a return path is recorded, not
scored.

T22-concurrent-clients keeps its own standard stack (two relays and one exit in a full mesh). Setup: `just
multihost-up HOSTS single-exit N` (or `just relay-topology single-exit N` on one machine), then `just
multihost-test t35` / `just test t35`. SKIP on any other stack."""
from suitelib import relaybench

TEST = "T35-single-exit-scaling"
KIND = "gate"
GROUP = "relayscale"
KNOBS = dict(relaybench.SCALING_KNOBS)


def TIMEOUT(knobs):
    return relaybench.timeout(knobs)


def test_single_exit_scaling(cfg, run, cluster, target, checks, knobs):
    relaybench.run_ladder(cfg, run, cluster, target, checks, knobs, "single-exit", under_test="exit")
