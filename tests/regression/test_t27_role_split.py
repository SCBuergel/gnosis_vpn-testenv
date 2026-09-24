"""T27-role-split (runbook): is a regression in the node acting as the exit or in the nodes acting as relays? Exit
on version A and relays on version B, then swapped, one T04-style run each. Always SKIP: hoprd-localcluster runs
one --hoprd-bin for every node (open extension 1, the highest-value one).

Why: it was the decisive test for the hoprd regression and it inverted the working hypothesis. Everyone assumed the
exit, which terminates sessions and runs the balancer; exit-new/relays-old was the best cell of the night and
exit-old/relays-new halved throughput both ways."""
TEST = "T27-role-split"
KIND = "runbook"
KNOBS = {}


def test_role_split(checks, knobs):
    checks.skip("needs per-role hoprd binaries in hoprd-localcluster (extension 1)")
