#!/usr/bin/env bash
# No `set`: this is an interactive tail, killed with Ctrl-C.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/common.sh"

: "${DATA_DIR:?}"

tail -f "${DATA_DIR}/logs/"*.log &
docker logs -f gnosis_vpn-client
