#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/common.sh"

: "${DOCKER_NETWORK:?}"
: "${DOCKER_NETWORK_SUBNET:?}"
: "${DOCKER_NETWORK_GATEWAY:?}"

create() {
    if docker network inspect "${DOCKER_NETWORK}" >/dev/null 2>&1; then
        echo "Docker network ${DOCKER_NETWORK} already exists — skipping create"
        return 0
    fi
    docker network create \
        --subnet "${DOCKER_NETWORK_SUBNET}" \
        --gateway "${DOCKER_NETWORK_GATEWAY}" \
        "${DOCKER_NETWORK}"
    echo "Created Docker network ${DOCKER_NETWORK} (subnet ${DOCKER_NETWORK_SUBNET}, gateway ${DOCKER_NETWORK_GATEWAY})"
}

case "${1:-}" in
create) create ;;
*) die "usage: network.sh create" ;;
esac
