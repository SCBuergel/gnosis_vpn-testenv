#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/common.sh"

: "${SERVER_COUNT:?}"

start() {
    : "${SERVER_LOG_LEVEL:?}"
    : "${SERVER_IMAGE:?}"
    : "${LOG_MAX_SIZE:?}"
    : "${LOG_MAX_FILE:?}"
    local i name wg_port api_port private_key running
    for i in $(seq 0 $((SERVER_COUNT - 1))); do
        name="gnosis_vpn-server-${i}"
        wg_port=$((51821 + i))
        api_port=$((8000 + i))
        if docker container inspect "${name}" >/dev/null 2>&1; then
            echo "${name} already exists — skipping start"
            echo "  WireGuard: ${wg_port}/udp, API: ${api_port}"
            continue
        fi
        private_key=$(wg genkey)
        docker run --rm --detach \
            --log-opt max-size="${LOG_MAX_SIZE}" --log-opt max-file="${LOG_MAX_FILE}" \
            --env "PRIVATE_KEY=${private_key}" \
            --env "RUST_LOG=${SERVER_LOG_LEVEL}" \
            --publish "${api_port}:8000" \
            --publish "${wg_port}:51820/udp" \
            --cap-add=NET_ADMIN \
            --add-host=host.docker.internal:host-gateway \
            --sysctl net.ipv4.conf.all.src_valid_mark=1 \
            --sysctl net.ipv4.ip_forward=1 \
            --name "${name}" \
            "${SERVER_IMAGE}"
        sleep 1
        running=$(docker inspect "${name}" 2>/dev/null | jq -r '.[0].State.Running // "false"')
        if [ "${running}" != "true" ]; then
            echo "Error: ${name} failed to start" >&2
            { docker logs "${name}" 2>&1 || true; } >&2
            exit 1
        fi
        echo "Started ${name} — WireGuard: ${wg_port}/udp, API: ${api_port}"
        # An extra WireGuard-side address the client's periodic liveness ping can answer (SERVER_PING_ALIAS).
        if [ -n "${SERVER_PING_ALIAS:-}" ]; then
            for _ in $(seq 1 30); do
                docker exec "${name}" ip link show wggvpn >/dev/null 2>&1 && break
                sleep 1
            done
            docker exec "${name}" ip addr add "${SERVER_PING_ALIAS}/32" dev wggvpn 2>/dev/null &&
                echo "  alias ${SERVER_PING_ALIAS} on wggvpn (client liveness-ping target)" ||
                echo "  warning: could not add ${SERVER_PING_ALIAS} to wggvpn; the client will reconnect every ~85 s" >&2
        fi
    done
}

stop() {
    local i
    for i in $(seq 0 $((SERVER_COUNT - 1))); do
        docker stop "gnosis_vpn-server-${i}" 2>/dev/null &&
            echo "Stopped gnosis_vpn-server-${i}" ||
            echo "gnosis_vpn-server-${i} was not running"
    done
}

case "${1:-}" in
start) start ;;
stop) stop ;;
*) die "usage: server.sh start|stop" ;;
esac
