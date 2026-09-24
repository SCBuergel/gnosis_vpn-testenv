"""T26-version-matrix (runbook): which component's version change causes a regression? Not a single-stack test:
`just matrix cells.txt` brings the stack up per cell (client image, hoprd binary, env) and runs the suite in it,
with a second pass in reverse order so wall clock and version are not confounded. This module only records which
cell it runs in.

Why: a 2x2 was the instrument that separated 'the client regressed' from 'the nodes regressed', and the reversed
repeat is what proved the effect was not drift after a real finding had been wrongly retracted on that suspicion."""
TEST = "T26-version-matrix"
KIND = "runbook"
KNOBS = {}


def test_version_matrix(cfg, checks, knobs):
    if cfg.cell:
        checks.record(f"cell '{cfg.cell}': client image {cfg.client_image}, hoprd {cfg.hoprd_bin}, env '{cfg.cluster_env}' "
                      f"(compare cells with the matrix summary)")
    else:
        checks.record("run through 'just matrix <cells>' to get a version matrix")
