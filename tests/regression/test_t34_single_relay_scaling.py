"""T34-single-relay-scaling (gate): how many concurrent clients can one relay carry? Rungs of LADDER="1 2 3 4 5":
n clients, each with its own exit, all through the same single relay in both directions (client k -> relay ->
exit k, and back over the relay), download DOWN_BYTES=100 MB at once, then upload UP_BYTES=100 MB at once. Its twin
is T35-single-exit-scaling (one exit): same ladder, same settings, same report. They do not read one-to-one: T34 puts all
traffic through one relay, while T35's exit spreads every client's return traffic over a pool of relays (see its
docstring). T33-relay-baseline runs the ladder with a relay and an exit per client.

Procedure (suitelib/relaybench.py): the channel graph is held against the topology before every rung (every client
and every exit exactly one Open channel, to the one relay); IDLE_S=10 after connecting (below the suite's
SURB_RAMP_WAIT floor on purpose, by request); every transfer of a phase starts at one common second (START_LEAD_S=5
after it is handed out; the start skew is reported); PAUSE_S=10 between download, upload and rungs. FAIL only when a
transfer does not complete within CAP=300 s (100 MB in 300 s is 2.67 Mbit/s). The rates are recorded, not scored, over
the overlap: the window in which every client was transferring, from the last first byte to the first last byte.
Reported per rung: the relay's machine CPU (% of all its cores) and its hoprd process's CPU (% of one core), the other
roles' machines, the hoprd and client versions and one line of machine specs per role. The relay must have forwarded
at least one packet per PKT_BYTES_MAX downloaded bytes (the share check of T33 is 100 % by construction here and only
recorded).

Setup: `just relay-topology shared N` on one machine, or `just multihost-up HOSTS shared N` across machines, then
`just test t34` / `just multihost-test t34`. SKIP on any other stack."""
from suitelib import relaybench

TEST = "T34-single-relay-scaling"
KIND = "gate"
GROUP = "relayscale"
KNOBS = dict(relaybench.SCALING_KNOBS)


def TIMEOUT(knobs):
    return relaybench.timeout(knobs)


def test_single_relay_scaling(cfg, run, cluster, target, checks, knobs):
    relaybench.run_ladder(cfg, run, cluster, target, checks, knobs, "shared", under_test="relay")
