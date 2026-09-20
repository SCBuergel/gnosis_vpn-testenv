"""T26-version-matrix (runbook): not a single-stack test. Run `just matrix cells.txt` with one line per cell; each
cell brings the stack up with its client image / hoprd binary / env and runs the suite. This only records which
cell it is running in."""
TEST = "T26-version-matrix"
KIND = "runbook"
KNOBS = {}


def test_version_matrix(cfg, checks, knobs):
    if cfg.cell:
        checks.record(f"cell '{cfg.cell}': client image {cfg.client_image}, hoprd {cfg.hoprd_bin}, env '{cfg.cluster_env}' "
                      f"(compare cells with the matrix summary)")
    else:
        checks.record("run through 'just matrix <cells>' to get a version matrix")
