#!/usr/bin/env bash
# No `-e`: `down` must finish tearing the stack down even when a step reports failure.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/common.sh"

: "${HOPRD_DIR:?}"
: "${CURVY_STACK_ENV:?}"

LAUNCHER="${HOPRD_DIR}/localcluster/scripts/curvy-localcluster.sh"

# hoprd's launcher does the work (`--stack-only`); what it hands back is the environment the
# nodes and the client need.
up() {
    : "${CONFIG_DIR:?}"
    : "${DOCKER_NETWORK_GATEWAY:?}"
    : "${CURVY_GATEWAY_PORT:?}"
    if [ -f "${CURVY_STACK_ENV}" ] && [ -n "$(docker compose --project-name hopr-curvy-stack ps -q gateway 2>/dev/null)" ]; then
        echo "Curvy stack already up — skipping (environment in ${CURVY_STACK_ENV})"
        return 0
    fi
    mkdir -p "${CONFIG_DIR}"

    local log env_file
    log=$(mktemp)
    CURVY_BIND_ADDR="${DOCKER_NETWORK_GATEWAY}" CURVY_GATEWAY_PORT="${CURVY_GATEWAY_PORT}" \
        "${LAUNCHER}" --stack-only 2>&1 | tee "${log}"
    env_file=$(sed -n 's/.*environment in \(.*stack\.env\)$/\1/p' "${log}" | tail -1)
    rm -f "${log}"
    [ -f "${env_file}" ] || die "Error: the Curvy launcher did not report its environment file"
    cp "${env_file}" "${CURVY_STACK_ENV}"
    echo "Curvy stack environment saved to ${CURVY_STACK_ENV}"
}

down() {
    if [ -f "${CURVY_STACK_ENV}" ] || [ -n "$(docker compose --project-name hopr-curvy-stack ps -aq 2>/dev/null)" ]; then
        [ -x "${LAUNCHER}" ] && "${LAUNCHER}" --down >/dev/null 2>&1
        echo "Curvy stack stopped"
    fi
    rm -f "${CURVY_STACK_ENV}"
}

case "${1:-}" in
up) up ;;
down) down ;;
*) die "usage: curvy.sh up|down" ;;
esac
