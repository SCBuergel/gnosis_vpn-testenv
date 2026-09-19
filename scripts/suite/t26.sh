#!/usr/bin/env bash
# T26-version-matrix — Version matrix across components. Not a single-stack test: run `just matrix cells.txt regression` with one
# line per cell (see scripts/suite/matrix.sh --help); each cell brings the stack up with its client image / hoprd
# binary / env and runs the profile. This script only records which cell it is running in.
source "$(dirname "$0")/lib.sh"
suite_kind runbook
[ "${1:-}" = "--help" ] && { sed -n '2,4p' "$0"; usage_common; exit 0; }
suite_init
[ -n "$SUITE_CELL" ] && record T26-version-matrix "cell '$SUITE_CELL': client image $CLIENT_IMAGE, hoprd $HOPRD_BIN, env '$CLUSTER_ENV' (compare cells with matrix.sh summary)" || record T26-version-matrix "run through 'just matrix <cells> <profile>' to get a version matrix"
exit 0
