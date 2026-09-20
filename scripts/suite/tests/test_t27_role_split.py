"""T27-role-split (runbook): exit version vs relay version. Blocked: hoprd-localcluster runs one --hoprd-bin for
every node (catalogue extension 1)."""
TEST = "T27-role-split"
KIND = "runbook"
KNOBS = {}


def test_role_split(checks, knobs):
    checks.skip("needs per-role hoprd binaries in hoprd-localcluster (extension 1)")
