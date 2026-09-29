#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/common.sh"

: "${CLIENT_STATE_DIR:?}"

# Start one client container: start [name [state_dir [extra_index]]]. Defaults are the primary client
# (gnosis_vpn-client, CLIENT_STATE_DIR, extra identity 0). Also starts the client's tools sidecar.
start() {
    : "${CONFIG_DIR:?}"
    : "${CLIENT_LOG_LEVEL:?}"
    : "${CLIENT_IMAGE:?}"
    : "${DOCKER_NETWORK:?}"
    : "${CLIENT_AUTOSTART:?}"
    : "${LOG_MAX_SIZE:?}"
    : "${LOG_MAX_FILE:?}"
    : "${SUITE_OUT_DIR:?}"
    local name="${1:-gnosis_vpn-client}"
    local state_dir="${2:-${CLIENT_STATE_DIR}}"
    local extra_index="${3:-0}"

    # a container stopped with --rm is removed asynchronously: only a *running* one counts as "already
    # there", a dying one is waited out (otherwise a restart right after client-stop silently starts nothing)
    if [ "$(docker inspect -f '{{.State.Running}}' "${name}" 2>/dev/null)" = "true" ]; then
        echo "${name} already running — skipping start"
        return 0
    fi
    for _ in $(seq 1 30); do
        docker container inspect "${name}" >/dev/null 2>&1 || break
        sleep 1
    done
    mkdir -p "${state_dir}" "${SUITE_OUT_DIR}"

    local blokli_url extra_id_pass running
    blokli_url=$(sed 's/localhost/host.docker.internal/' "${CONFIG_DIR}/blokli_url")
    extra_id_pass=$(cat "${CONFIG_DIR}/extra_id_${extra_index}.password")
    # the identity lives in the writable state dir: a newer client migrates an older keystore format in
    # place, which fails on the read-only /config mount ("Read-only file system", worker exit 71)
    cp "${CONFIG_DIR}/extra_id_${extra_index}.id" "${state_dir}/identity.id"

    local extra_env=()
    # shellcheck disable=SC2086  # intentional word-splitting of the "K=V K=V" list
    for kv in ${CLIENT_EXTRA_ENV:-}; do extra_env+=(--env "${kv}"); done
    local sysctl_args=()
    [ -n "${CLIENT_SYSCTL:-}" ] && sysctl_args=(--sysctl "${CLIENT_SYSCTL}")
    local extra_args=()
    # shellcheck disable=SC2086  # intentional word-splitting of the extra-args list
    for a in ${CLIENT_EXTRA_ARGS:-}; do extra_args+=("${a}"); done

    # The client's embedded node is the PIX Entry, so under the Curvy pool it needs what the nodes
    # get: the pool's HOPRD_CURVY_* overrides, and the proving keys it allocates deposits with.
    local curvy_args=()
    if [ "${CLUSTER_PIX_POOL:-}" = "curvy" ]; then
        load_curvy_env
        curvy_args=(
            --env HOPRD_CURVY_SHIELDING --env HOPRD_CURVY_SUBMISSION --env HOPRD_CURVY_RELAYER_URL
            --env HOPRD_CURVY_NOTE_SOURCE --env HOPRD_CURVY_TOKEN
            --env CURVY_ZK_KEYS_DIR=/curvy-zk --volume "${CURVY_ZK_KEYS_DIR}:/curvy-zk:ro"
        )
    fi

    docker run --detach --rm \
        --name "${name}" \
        --network "${DOCKER_NETWORK}" \
        --cap-add=NET_ADMIN \
        --device /dev/net/tun \
        --add-host=host.docker.internal:host-gateway \
        --log-opt max-size="${LOG_MAX_SIZE}" --log-opt max-file="${LOG_MAX_FILE}" \
        --sysctl net.ipv4.ping_group_range="0 2147483647" \
        "${sysctl_args[@]}" \
        --env RUST_LOG="${CLIENT_LOG_LEVEL}" \
        --env GNOSISVPN_CONFIG_PATH=/config/client.toml \
        --env GNOSISVPN_HOPR_BLOKLI_URL="${blokli_url}" \
        --env GNOSISVPN_HOPR_IDENTITY_FILE=/var/lib/gnosisvpn/identity.id \
        --env GNOSISVPN_HOPR_IDENTITY_PASS="${extra_id_pass}" \
        --env GNOSISVPN_HOME=/var/lib/gnosisvpn \
        --env GNOSISVPN_CLIENT_AUTOSTART="${CLIENT_AUTOSTART}" \
        "${extra_env[@]}" \
        --volume "${CONFIG_DIR}:/config:ro" \
        --volume "${state_dir}:/var/lib/gnosisvpn" \
        "${curvy_args[@]}" \
        "${CLIENT_IMAGE}" "${extra_args[@]}"
    sleep 1
    running=$(docker inspect "${name}" 2>/dev/null | jq -r '.[0].State.Running // "false"')
    if [ "${running}" != "true" ]; then
        echo "Error: ${name} failed to start" >&2
        { docker logs "${name}" 2>&1 || true; } >&2
        exit 1
    fi
    echo "Started ${name} (${CLIENT_IMAGE}, identity extra_id_${extra_index})"
    tools_start "${name}"
}

# The tools sidecar of one client: joins the client's network namespace (tunnel interface, routes,
# sysctls), carries curl, ping, ip and python for the probes, mounts tests/ (the probes) and the run
# directory. Removed with the client.
tools_start() {
    : "${TOOLS_IMAGE:?}"
    : "${REPO_DIR:?}"
    : "${SUITE_OUT_DIR:?}"
    local name="${1:?usage: client.sh tools-start <name>}"
    docker image inspect "${TOOLS_IMAGE}" >/dev/null 2>&1 || just build-tools
    docker rm -f "${name}-tools" >/dev/null 2>&1 || true
    docker run --detach --rm \
        --name "${name}-tools" \
        --network "container:${name}" \
        --cap-add=NET_ADMIN --cap-add=NET_RAW \
        --log-opt max-size=10m --log-opt max-file=2 \
        --volume "${REPO_DIR}/tests:/suite:ro" \
        --volume "${SUITE_OUT_DIR}:/suite-out" \
        "${TOOLS_IMAGE}" >/dev/null
    echo "Started ${name}-tools (${TOOLS_IMAGE}, network namespace of ${name})"
}

# Start CLIENT_COUNT clients: the primary plus extras 2..N on extra identities 1..N-1 (T22 ladder).
clients_start() {
    : "${CLIENT_COUNT:?}"
    start gnosis_vpn-client "${CLIENT_STATE_DIR}" 0
    local i
    for i in $(seq 2 "${CLIENT_COUNT}"); do
        start "gnosis_vpn-client-${i}" "${CLIENT_STATE_DIR}-${i}" "$((i - 1))"
    done
    echo "clients running: ${CLIENT_COUNT}"
}

# Stop one extra client container: its tools sidecar, the container (waited out), then its state dir.
stop_extra() {
    local name="${1:?usage: client.sh stop-extra <name> <state_dir>}"
    local state_dir="${2:?usage: client.sh stop-extra <name> <state_dir>}"
    docker rm -f "${name}-tools" >/dev/null 2>&1 || true
    docker stop "${name}" 2>/dev/null || true
    for _ in $(seq 1 30); do
        docker container inspect "${name}" >/dev/null 2>&1 || break
        sleep 1
    done
    rm -rf "${state_dir}" 2>/dev/null || sudo rm -rf "${state_dir}" 2>/dev/null || true
}

# Stop every extra client container (2..16); the primary is left to `stop`.
clients_stop() {
    local i name
    for i in $(seq 2 16); do
        name="gnosis_vpn-client-${i}"
        docker rm -f "${name}-tools" >/dev/null 2>&1 || true
        docker container inspect "${name}" >/dev/null 2>&1 || continue
        docker stop "${name}" >/dev/null 2>&1 || true
        for _ in $(seq 1 30); do
            docker container inspect "${name}" >/dev/null 2>&1 || break
            sleep 1
        done
        rm -rf "${CLIENT_STATE_DIR}-${i}" 2>/dev/null || sudo rm -rf "${CLIENT_STATE_DIR}-${i}" 2>/dev/null || true
    done
}

# Stop the client wherever it is running — container or host-native.
stop() {
    docker rm -f gnosis_vpn-client-tools >/dev/null 2>&1 || true
    docker stop gnosis_vpn-client 2>/dev/null || true
    # --rm removal is asynchronous; wait for it so a following client-start does not see a dying container
    for _ in $(seq 1 30); do
        docker container inspect gnosis_vpn-client >/dev/null 2>&1 || break
        sleep 1
    done
    # only touch sudo if a host-native client is actually running, so the container-only workflow (the
    # common case) never hits a sudo prompt here. Match the host-native binary path, not the bare name:
    # container processes (a second client) are visible to pgrep too.
    local native="${GVPN_CLIENT_DIR:-}/result/bin/gnosis_vpn-"
    if pgrep -f "^${native}root" >/dev/null 2>&1 || pgrep -f "^${native}worker" >/dev/null 2>&1; then
        sudo pkill -f "^${native}root" 2>/dev/null || true
        sudo pkill -f "^${native}worker" 2>/dev/null || true
    fi
    true
}

start_on_host() {
    : "${CONFIG_DIR:?}"
    : "${GVPN_CLIENT_DIR:?}"
    : "${CLIENT_WORKER_USER:?}"
    : "${CLIENT_LOG_LEVEL:?}"
    : "${CLIENT_LOG_FILE:?}"
    local root_bin="${GVPN_CLIENT_DIR}/result/bin/gnosis_vpn-root"
    local worker_bin="${GVPN_CLIENT_DIR}/result/bin/gnosis_vpn-worker"
    if [ ! -f "${root_bin}" ] || [ ! -f "${worker_bin}" ]; then
        die "Error: gnosis_vpn-client binaries not found at ${GVPN_CLIENT_DIR}/result/bin/" \
            "Run 'just build-client-native' to build them first"
    fi
    id "${CLIENT_WORKER_USER}" >/dev/null 2>&1 || die \
        "Error: worker user '${CLIENT_WORKER_USER}' not found on this host." \
        "gnosis_vpn-root drops privileges to this user (by uid/gid) when spawning gnosis_vpn-worker," \
        "so it must already exist as a system account (its home directory is irrelevant — create" \
        "one via your NixOS config, or override the name via CLIENT_WORKER_USER)."
    if pgrep -f gnosis_vpn-root >/dev/null 2>&1; then
        echo "Client already running on host — skipping start"
        return 0
    fi
    mkdir -p "${CLIENT_STATE_DIR}"

    local blokli_url extra_id_pass
    blokli_url=$(cat "${CONFIG_DIR}/blokli_url")
    extra_id_pass=$(cat "${CONFIG_DIR}/extra_id.password")

    # sudo backgrounded can't read TTY; pre-authenticate while still interactive
    sudo -v
    sudo RUST_LOG="${CLIENT_LOG_LEVEL}" \
        GNOSISVPN_CONFIG_PATH="${CONFIG_DIR}/client.toml" \
        GNOSISVPN_HOPR_BLOKLI_URL="${blokli_url}" \
        GNOSISVPN_HOPR_IDENTITY_FILE="${CONFIG_DIR}/extra_id.id" \
        GNOSISVPN_HOPR_IDENTITY_PASS="${extra_id_pass}" \
        GNOSISVPN_HOME="${CLIENT_STATE_DIR}" \
        GNOSISVPN_CLIENT_AUTOSTART=30min \
        GNOSISVPN_WORKER_USER="${CLIENT_WORKER_USER}" \
        GNOSISVPN_LOG_FILE="${CLIENT_LOG_FILE}" \
        "${root_bin}" --worker-binary "${worker_bin}" &
    echo "Client PID: $!"
}

# Cascades SIGTERM to the worker via gnosis_vpn-root.
stop_on_host() {
    sudo pkill -f gnosis_vpn-root 2>/dev/null || true
    sudo pkill -f gnosis_vpn-worker 2>/dev/null || true
    echo "Client (host) stopped"
}

# Escalates only when it has to. The *container's* entrypoint chowns this bind-mounted dir to its
# internal worker uid, so the host user cannot remove it afterwards — but when the client ran on the
# host, or never ran at all, the directory is plainly removable or absent. Asking for a password
# there failed the whole `down` on a shell without a tty, after everything else had already stopped.
purge_state() {
    if [ ! -e "${CLIENT_STATE_DIR}" ]; then
        echo "${CLIENT_STATE_DIR} does not exist — nothing to purge"
        return 0
    fi
    if rm -rf "${CLIENT_STATE_DIR}" 2>/dev/null; then
        echo "Purged ${CLIENT_STATE_DIR}"
        return 0
    fi
    sudo rm -rf "${CLIENT_STATE_DIR}"
    echo "Purged ${CLIENT_STATE_DIR} (needed sudo)"
}

purge_state_interactive() {
    local answer
    read -r -p "Permanently delete '${CLIENT_STATE_DIR}'? Type 'yes' to confirm: " answer
    [ "${answer}" = "yes" ] || die "Aborted"
    sudo rm -rf "${CLIENT_STATE_DIR}"
    echo "Purged ${CLIENT_STATE_DIR}"
}

case "${1:-}" in
start)
    shift
    start "$@"
    ;;
stop) stop ;;
stop-extra)
    shift
    stop_extra "$@"
    ;;
clients-start) clients_start ;;
clients-stop) clients_stop ;;
tools-start)
    shift
    tools_start "$@"
    ;;
start-on-host) start_on_host ;;
stop-on-host) stop_on_host ;;
purge-state) purge_state ;;
purge-state-interactive) purge_state_interactive ;;
*) die "usage: client.sh start [name state_dir extra_index]|stop|stop-extra <name> <state_dir>|clients-start|clients-stop|tools-start <name>|start-on-host|stop-on-host|purge-state|purge-state-interactive" ;;
esac
