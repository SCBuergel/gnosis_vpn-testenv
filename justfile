# Paths to sibling repos — override via env in CI
HOPRD_DIR       := env_var_or_default("HOPRD_DIR",       "../hoprd")
GVPN_SERVER_DIR := env_var_or_default("GVPN_SERVER_DIR", "../gnosis_vpn-server")
GVPN_CLIENT_DIR := env_var_or_default("GVPN_CLIENT_DIR", "../gnosis_vpn-client")

# Localcluster settings
CLUSTER_SIZE := env_var_or_default("CLUSTER_SIZE", "3")
# Binaries the cluster runs. Defaults are the nix out-links of build-cluster; override to run a
# stock release binary, a cargo build, or a different hoprd version per cell (regression suite T25-knob-ab/T26-version-matrix).
HOPRD_BIN        := env_var_or_default("HOPRD_BIN",        HOPRD_DIR + "/result-hoprd/bin/hoprd")
LOCALCLUSTER_BIN := env_var_or_default("LOCALCLUSTER_BIN", HOPRD_DIR + "/result-localcluster/bin/hoprd-localcluster")
# Extra environment for every cluster node, "K=V K=V" (e.g. HOPR_INTERNAL_IN_PACKET_PIPELINE_CONCURRENCY=64)
CLUSTER_ENV := env_var_or_default("CLUSTER_ENV", "")
# Artificial inter-node latency, passed to hoprd-localcluster --latency ("50ms", "100ms±30ms", "config:/path.yaml")
CLUSTER_LATENCY := env_var_or_default("CLUSTER_LATENCY", "")
# Channel management mode (api|strategy|both|none) and per-channel funding
CLUSTER_CHANNEL_MANAGEMENT := env_var_or_default("CLUSTER_CHANNEL_MANAGEMENT", "api")
CLUSTER_FUNDING := env_var_or_default("CLUSTER_FUNDING", "1 wxHOPR")
# Concurrent client containers for the T22-concurrent-clients ladder. Client 1 uses extra identity 0, client N uses N-1, so the
# cluster must be created with at least CLIENT_COUNT pre-funded identities — which is why EXTRA_IDENTITIES
# defaults to it. Changing CLIENT_COUNT after the cluster is up needs a cluster restart to mint the identities.
CLIENT_COUNT := env_var_or_default("CLIENT_COUNT", "1")
# Pre-funded extra identities: one per concurrent client (index 0 is the primary client)
EXTRA_IDENTITIES := env_var_or_default("EXTRA_IDENTITIES", CLIENT_COUNT)
# cluster-wait gives up (exit 1) after this many seconds, or as soon as the localcluster reports 'failed' / is gone
CLUSTER_WAIT_TIMEOUT := env_var_or_default("CLUSTER_WAIT_TIMEOUT", "900")
DATA_DIR     := env_var_or_default("DATA_DIR",     "/tmp/hopr-nodes")
CHAIN_IMAGE  := env_var_or_default("CHAIN_IMAGE",  "europe-west3-docker.pkg.dev/hoprassociation/docker-images/bloklid-anvil:latest")

# Docker network the client container joins to reach the (host-native) localcluster.
# Fixed subnet so the gateway IP — what the cluster binds/announces its P2P host as — is deterministic.
DOCKER_NETWORK         := env_var_or_default("DOCKER_NETWORK",         "gnosis-vpn-testenv")
DOCKER_NETWORK_SUBNET  := env_var_or_default("DOCKER_NETWORK_SUBNET",  "172.30.0.0/24")
# Extra address the server holds on its WireGuard interface, so the client's periodic tunnel-liveness ping is
# answered. Client <= 0.96.3 pings the hardcoded default 10.128.0.1 every 10 s (gnosis_vpn-lib core/runner.rs
# tunnel_ping_loop uses ping::Options::default(), ignoring [connection.ping]) while the server image sits at
# 10.129.0.1; three misses tear the tunnel down, so every session reconnected every ~85 s. Empty disables.
SERVER_PING_ALIAS      := env_var_or_default("SERVER_PING_ALIAS", "10.128.0.1")
DOCKER_NETWORK_GATEWAY := env_var_or_default("DOCKER_NETWORK_GATEWAY", "172.30.0.1")

# VPN server settings
SERVER_COUNT := env_var_or_default("SERVER_COUNT", "1")
SERVER_IMAGE := env_var_or_default("SERVER_IMAGE", "gnosis_vpn-server")

# Client image, keepalive, and per-cell knobs for the regression suite
CLIENT_IMAGE      := env_var_or_default("CLIENT_IMAGE", "gnosis_vpn-client")
CLIENT_AUTOSTART  := env_var_or_default("CLIENT_AUTOSTART", "30min")
CLIENT_EXTRA_ARGS := env_var_or_default("CLIENT_EXTRA_ARGS", "")   # appended to gnosis_vpn-root, e.g. "--allow-insecure" (T30-hopcount-ab)
CLIENT_EXTRA_ENV  := env_var_or_default("CLIENT_EXTRA_ENV", "")    # "K=V K=V", e.g. GNOSISVPN_SURB_RAMP_SECS=0 (T25-knob-ab)
CLIENT_SYSCTL     := env_var_or_default("CLIENT_SYSCTL", "")       # e.g. net.ipv4.tcp_congestion_control=bbr (T32-congestion-control)

# Bounded container logs (soak safety)
LOG_MAX_SIZE := env_var_or_default("LOG_MAX_SIZE", "300m")
LOG_MAX_FILE := env_var_or_default("LOG_MAX_FILE", "3")

# Also generate 0-hop destinations (node-N-h0) next to the HOPS ones — needs CLIENT_EXTRA_ARGS=--allow-insecure (T30-hopcount-ab)
HOPS0_ALSO := env_var_or_default("HOPS0_ALSO", "0")

# In-cluster traffic target and suite output.
# The target sits on its own Docker network with a NON-private subnet: the client keeps RFC1918 ranges off the
# tunnel, so a target on Docker's 172.17/16 bridge would be routed around the exit. 198.18.0.0/15 is the
# RFC 2544 benchmarking range; every exit server is attached to this network and NATs into it.
TARGET_IMAGE   := env_var_or_default("TARGET_IMAGE", "gnosis_vpn-target")
TARGET_NAME    := env_var_or_default("TARGET_NAME", "gnosis_vpn-target")
TARGET_NETWORK := env_var_or_default("TARGET_NETWORK", "gnosis-vpn-target")
TARGET_SUBNET  := env_var_or_default("TARGET_SUBNET", "198.18.0.0/24")
SUITE_OUT_DIR := env_var_or_default("SUITE_OUT_DIR", "/tmp/gnosis_vpn-testenv-suite")

# Override for _lan-ip's auto-detection (multi-NIC hosts, or when the default route is wrong)
LAN_IP := env_var_or_default("LAN_IP", "")

# Data directory for VictoriaMetrics on-disk storage
METRICS_DATA_DIR := env_var_or_default("METRICS_DATA_DIR", "/tmp/hopr-metrics-data")

# Session hop count for destinations (0 = direct, 1+ = via relays)
HOPS := env_var_or_default("HOPS", "1")

# Log levels for each component (passed as RUST_LOG)
CLIENT_LOG_LEVEL  := env_var_or_default("CLIENT_LOG_LEVEL",  "warn,gnosis_vpn_root=debug,gnosis_vpn_lib=debug,gnosis_vpn_worker=debug")
SERVER_LOG_LEVEL  := env_var_or_default("SERVER_LOG_LEVEL",  "info")
CLUSTER_LOG_LEVEL := env_var_or_default("CLUSTER_LOG_LEVEL", "info")

# Persistent worker state (identity keys, cache) bind-mounted into the client container
CLIENT_STATE_DIR := env_var_or_default("CLIENT_STATE_DIR", "/tmp/gnosis_vpn-testenv-state")

# OS user gnosis_vpn-root drops privileges to when spawning gnosis_vpn-worker (host-native client only; must already exist)
CLIENT_WORKER_USER := env_var_or_default("CLIENT_WORKER_USER", "gnosisvpntestenv")

# Client log file path (host-native client only — the container relies on `docker logs` instead)
CLIENT_LOG_FILE := env_var_or_default("CLIENT_LOG_FILE", "/tmp/gnosis_vpn-client.log")

# End-to-end browser test suite (see e2e/README.md)
E2E_IMAGE   := env_var_or_default("E2E_IMAGE",   "gnosis_vpn-e2e")
E2E_OUT_DIR := env_var_or_default("E2E_OUT_DIR", "/tmp/gnosis_vpn-testenv-e2e")

# Generated config output dir
CONFIG_DIR    := env_var_or_default("CONFIG_DIR", "/tmp/gnosis_vpn-testenv")
TEMPLATES_DIR := justfile_directory() + "/templates"

# Files a remote client needs, bundled together for up-on-network (see gen-config-on-network)
NETWORK_BUNDLE_DIR := CONFIG_DIR + "/on-network"

# List available recipes
default:
    @just --list

# ─── Build ───────────────────────────────────────────────────────────────────

# Build hoprd and hoprd-localcluster binaries via nix
build-cluster:
    nix build -L --out-link {{HOPRD_DIR}}/result-hoprd        {{HOPRD_DIR}}#binary-hoprd
    nix build -L --out-link {{HOPRD_DIR}}/result-localcluster {{HOPRD_DIR}}#binary-hoprd-localcluster

# Build gnosis_vpn-server Docker image
build-server:
    cd {{GVPN_SERVER_DIR}} && just docker-build

# Build gnosis_vpn-client Docker image
build-client:
    cd {{GVPN_CLIENT_DIR}} && just docker-build

# Build gnosis_vpn-client binaries only (no Docker image) — for the host-native client
build-client-native:
    cd {{GVPN_CLIENT_DIR}} && just build

# Build all components
build: build-cluster build-server build-client

# Build the in-cluster traffic target image (sized HTTP target, UDP echo, stream server, call server)
build-target:
    docker build -q -t "{{TARGET_IMAGE}}" "{{justfile_directory()}}/docker/target" && echo "built {{TARGET_IMAGE}}"

# ─── Networking ──────────────────────────────────────────────────────────────

# Create the fixed-subnet Docker network joining the client container to the host-native localcluster
network-create:
    #!/usr/bin/env bash
    set -euo pipefail
    if docker network inspect "{{DOCKER_NETWORK}}" > /dev/null 2>&1; then
        echo "Docker network {{DOCKER_NETWORK}} already exists — skipping create"
    else
        docker network create --subnet "{{DOCKER_NETWORK_SUBNET}}" --gateway "{{DOCKER_NETWORK_GATEWAY}}" "{{DOCKER_NETWORK}}"
        echo "Created Docker network {{DOCKER_NETWORK}} (subnet {{DOCKER_NETWORK_SUBNET}}, gateway {{DOCKER_NETWORK_GATEWAY}})"
    fi

# Remove the Docker network
network-remove:
    docker network rm "{{DOCKER_NETWORK}}" 2>/dev/null || true

# ─── Localcluster ────────────────────────────────────────────────────────────

# Shared start/restart logic for the three cluster-start* variants below: binary checks, skip-if-already-running-on-this-host, restart-if-running-on-a-different-host, spawn.
_cluster-start p2p_host:
    #!/usr/bin/env bash
    set -euo pipefail
    lc_bin="{{LOCALCLUSTER_BIN}}"
    hoprd_bin="{{HOPRD_BIN}}"
    if [ ! -f "${lc_bin}" ]; then
        echo "Error: hoprd-localcluster not found at ${lc_bin}" >&2
        echo "Run 'just build-cluster' to build it first" >&2
        exit 1
    fi
    if [ ! -f "${hoprd_bin}" ]; then
        echo "Error: hoprd not found at ${hoprd_bin}" >&2
        echo "Run 'just build-cluster' to build it first" >&2
        exit 1
    fi
    p2p_host="{{p2p_host}}"
    cluster_state=$("${lc_bin}" status --data-dir "{{DATA_DIR}}" 2>/dev/null | jq -r '.state // "not_running"')
    if [ "${cluster_state}" = "failed" ]; then
        echo "Cluster is in state 'failed' — run 'just cluster-stop' to clean up before restarting"
        exit 1
    fi
    if [ "${cluster_state}" != "not_running" ]; then
        current_host=$(just _cluster-p2p-host)
        if [ "${current_host}" = "${p2p_host}" ]; then
            pid=$(pgrep -f hoprd-localcluster | head -1)
            echo "Cluster found in state '${cluster_state}' (PID ${pid}), already on P2P host ${p2p_host} — skipping start"
            exit 0
        fi
        echo "Cluster is running with P2P host '${current_host}', but this recipe needs '${p2p_host}' — restarting with the correct host"
        just cluster-stop
    fi
    latency_args=()
    [ -n "{{CLUSTER_LATENCY}}" ] && latency_args=(--latency "{{CLUSTER_LATENCY}}")
    # CLUSTER_ENV is inherited by every hoprd the localcluster spawns (per-node knobs, catalogue T25-knob-ab).
    # setsid/nohup: the cluster must outlive the shell (or systemd unit) that ran this recipe.
    mkdir -p "{{DATA_DIR}}/logs"
    setsid nohup env {{CLUSTER_ENV}} RUST_LOG={{CLUSTER_LOG_LEVEL}} \
        "${lc_bin}" \
        --hoprd-bin   "${hoprd_bin}" \
        --chain-image "{{CHAIN_IMAGE}}" \
        --size        {{CLUSTER_SIZE}} \
        --p2p-host    "${p2p_host}" \
        --data-dir    "{{DATA_DIR}}" \
        --channel-management {{CLUSTER_CHANNEL_MANAGEMENT}} \
        --funding-amount "{{CLUSTER_FUNDING}}" \
        --extra-identities {{EXTRA_IDENTITIES}} \
        "${latency_args[@]}" > "{{DATA_DIR}}/logs/localcluster.log" 2>&1 &
    echo "Localcluster PID: $! (log {{DATA_DIR}}/logs/localcluster.log) (P2P on ${p2p_host}; hoprd ${hoprd_bin}; env '{{CLUSTER_ENV}}'; latency '{{CLUSTER_LATENCY}}')"

# Start localcluster (--extra-identities 1 pre-funds the client identity; P2P binds to the Docker gateway IP)
cluster-start: network-create
    just _cluster-start {{DOCKER_NETWORK_GATEWAY}}

# Start localcluster for a host-native client (see up-client-on-host); P2P binds to loopback instead of the Docker gateway
cluster-start-on-host:
    just _cluster-start 127.0.0.1

# Start localcluster reachable from other machines on the LAN (see up-on-network); P2P binds/announces LAN_IP
cluster-start-on-network:
    just _cluster-start "$(just _lan-ip)"

# Poll until cluster reaches state=running
cluster-wait:
    #!/usr/bin/env bash
    set -euo pipefail
    lc_bin="{{LOCALCLUSTER_BIN}}"
    if [ ! -f "${lc_bin}" ]; then
        echo "Error: hoprd-localcluster not found at ${lc_bin}" >&2
        echo "Run 'just build-cluster' to build it first" >&2
        exit 1
    fi
    echo "Waiting for cluster..."
    waited=0
    until [ "$("${lc_bin}" status --data-dir "{{DATA_DIR}}" 2>/dev/null | jq -r '.state // empty')" = "running" ]; do
        state=$("${lc_bin}" status --data-dir "{{DATA_DIR}}" 2>/dev/null | jq -r '.state // "not_running"')
        if [ "${state}" = "failed" ] || { [ "${state}" = "not_running" ] && [ "${waited}" -ge 30 ] && ! pgrep -f '^[^ ]*hoprd-localcluster( |$)' > /dev/null; }; then
            echo "Error: cluster ${state} — see {{DATA_DIR}}/logs/localcluster.log" >&2
            tail -5 "{{DATA_DIR}}/logs/localcluster.log" >&2 2>/dev/null || true
            exit 1
        fi
        if [ "${waited}" -ge {{CLUSTER_WAIT_TIMEOUT}} ]; then
            echo "Error: cluster not running after {{CLUSTER_WAIT_TIMEOUT}}s (state ${state})" >&2
            exit 1
        fi
        sleep 1; waited=$((waited + 1))
    done
    echo "Cluster running"

# Print live cluster status as JSON
cluster-status:
    #!/usr/bin/env bash
    set -euo pipefail
    lc_bin="{{LOCALCLUSTER_BIN}}"
    if [ ! -f "${lc_bin}" ]; then
        echo "Error: hoprd-localcluster not found at ${lc_bin}" >&2
        echo "Run 'just build-cluster' to build it first" >&2
        exit 1
    fi
    "${lc_bin}" status --data-dir "{{DATA_DIR}}"

# Stop localcluster
cluster-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    # anchored so a caller whose own command line mentions these paths (a wrapper script, a cargo build) is not killed
    pkill -f '^[^ ]*hoprd-localcluster( |$)' 2>/dev/null || true
    pkill -f '^[^ ]*result-hoprd/bin/hoprd( |$)' 2>/dev/null || true
    pkill -f '^{{HOPRD_BIN}}( |$)' 2>/dev/null || true
    docker rm -f hopr-chain 2>/dev/null || true
    # wait for the chain container to actually go away before returning: a following cluster-start otherwise
    # races a still-dying Anvil and connects to a half-up chain ("chain subscription stream ended ... degraded"
    # -> "insufficient token balance at the signer"). This is what made cluster-restart unreliable.
    for i in $(seq 1 30); do
        docker container inspect hopr-chain > /dev/null 2>&1 || break
        sleep 1
    done
    # cluster recreates state everytime, so we can safely delete it on stop
    rm -rf "{{DATA_DIR}}"
    echo "Cluster stopped"

# ─── VPN Servers ─────────────────────────────────────────────────────────────

# Start SERVER_COUNT gnosis_vpn-server containers (server-i: WireGuard 51821+i/udp, API 8000+i)
server-start:
    #!/usr/bin/env bash
    set -euo pipefail
    for i in $(seq 0 $(({{SERVER_COUNT}} - 1))); do
        name="gnosis_vpn-server-${i}"
        wg_port=$((51821 + i))
        api_port=$((8000 + i))
        if docker container inspect "${name}" > /dev/null 2>&1; then
            echo "${name} already exists — skipping start"
            echo "  WireGuard: ${wg_port}/udp, API: ${api_port}"
            continue
        fi
        private_key=$(wg genkey)
        docker run --rm --detach \
            --log-opt max-size={{LOG_MAX_SIZE}} --log-opt max-file={{LOG_MAX_FILE}} \
            --env  "PRIVATE_KEY=${private_key}" \
            --env  "RUST_LOG={{SERVER_LOG_LEVEL}}" \
            --publish "${api_port}:8000" \
            --publish "${wg_port}:51820/udp" \
            --cap-add=NET_ADMIN \
            --add-host=host.docker.internal:host-gateway \
            --sysctl net.ipv4.conf.all.src_valid_mark=1 \
            --sysctl net.ipv4.ip_forward=1 \
            --name "${name}" \
            {{SERVER_IMAGE}}
        sleep 1
        running=$(docker inspect "${name}" 2>/dev/null | jq -r '.[0].State.Running // "false"')
        if [ "${running}" != "true" ]; then
            echo "Error: ${name} failed to start" >&2
            { docker logs "${name}" 2>&1 || true; } >&2
            exit 1
        fi
        echo "Started ${name} — WireGuard: ${wg_port}/udp, API: ${api_port}"
        if [ -n "{{SERVER_PING_ALIAS}}" ]; then
            for _ in $(seq 1 30); do docker exec "${name}" ip link show wggvpn >/dev/null 2>&1 && break; sleep 1; done
            docker exec "${name}" ip addr add "{{SERVER_PING_ALIAS}}/32" dev wggvpn 2>/dev/null \
                && echo "  alias {{SERVER_PING_ALIAS}} on wggvpn (client liveness-ping target)" \
                || echo "  warning: could not add {{SERVER_PING_ALIAS}} to wggvpn; the client will reconnect every ~85 s" >&2
        fi
    done

# Stop all VPN server containers
server-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    for i in $(seq 0 $(({{SERVER_COUNT}} - 1))); do
        docker stop "gnosis_vpn-server-${i}" 2>/dev/null \
            && echo "Stopped gnosis_vpn-server-${i}" \
            || echo "gnosis_vpn-server-${i} was not running"
    done

# ─── Config generation ───────────────────────────────────────────────────────

# Derive client config and system-test artifacts from live cluster status
gen-config:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "{{CONFIG_DIR}}"
    lc_bin="{{LOCALCLUSTER_BIN}}"

    status=$("${lc_bin}" status --data-dir "{{DATA_DIR}}")
    blokli_url=$(echo "${status}" | jq -r '.blokli_url')

    # One [destinations.node-N] block per cluster exit node
    destinations=""
    while IFS= read -r node; do
        id=$(echo "${node}"      | jq -r '.id')
        address=$(echo "${node}" | jq -r '.address')
        block=$(DEST_ID="${id}" DEST_ADDRESS="${address}" DEST_HOPS="{{HOPS}}" \
            envsubst '$DEST_ID,$DEST_ADDRESS,$DEST_HOPS' \
            < "{{TEMPLATES_DIR}}/destination.toml.tpl")
        destinations+="${block}"$'\n'
        if [ "{{HOPS0_ALSO}}" = "1" ]; then
            block=$(DEST_ID="${id}-h0" DEST_ADDRESS="${address}" DEST_HOPS="0" \
                envsubst '$DEST_ID,$DEST_ADDRESS,$DEST_HOPS' \
                < "{{TEMPLATES_DIR}}/destination.toml.tpl")
            destinations+="${block}"$'\n'
        fi
    done < <(echo "${status}" | jq -c '.nodes[]')

    DESTINATIONS="${destinations}" \
        envsubst '$DESTINATIONS' \
        < "{{TEMPLATES_DIR}}/client.toml.tpl" \
        > "{{CONFIG_DIR}}/client.toml"

    echo "${blokli_url}" > "{{CONFIG_DIR}}/blokli_url"
    echo "Generated {{CONFIG_DIR}}/client.toml"

    # Persist the extra identity artifacts needed by client and system tests.
    # extra_id.* is extra 0 (the client); extra_id_<i>.* every extra (extra 1 = second client, T22-concurrent-clients/T19-background-load/T21-passive-observer)
    echo "${status}" | jq -c '.extras[]' | while IFS= read -r extra; do
        i=$(echo "${extra}" | jq -r '.id')
        keystore_path=$(echo "${extra}" | jq -r '.keystore_path')
        cp "${keystore_path}"                       "{{CONFIG_DIR}}/extra_id_${i}.id"
        echo "${extra}" | jq -r '.password'       > "{{CONFIG_DIR}}/extra_id_${i}.password"
        echo "${extra}" | jq -r '.safe_address'   > "{{CONFIG_DIR}}/extra_id_${i}.safe"
        echo "${extra}" | jq -r '.module_address' > "{{CONFIG_DIR}}/extra_id_${i}.module"
        if [ "${i}" = "0" ]; then
            for ext in id password safe module; do cp "{{CONFIG_DIR}}/extra_id_0.${ext}" "{{CONFIG_DIR}}/extra_id.${ext}"; done
        fi
        echo "Saved extra identity ${i} artifacts to {{CONFIG_DIR}}"
    done

# Derive a LAN-reachable client config + blokli URL for a client running on another machine (see up-on-network)
gen-config-on-network: gen-config
    #!/usr/bin/env bash
    set -euo pipefail
    lan_ip=$(just _lan-ip)
    sed "s/127\.0\.0\.1/${lan_ip}/g" "{{CONFIG_DIR}}/client.toml" > "{{CONFIG_DIR}}/client-on-network.toml"
    sed "s/localhost/${lan_ip}/"     "{{CONFIG_DIR}}/blokli_url"   > "{{CONFIG_DIR}}/blokli_url-on-network"
    mkdir -p "{{NETWORK_BUNDLE_DIR}}"
    cp "{{CONFIG_DIR}}/client-on-network.toml" "{{CONFIG_DIR}}/extra_id.id" "{{CONFIG_DIR}}/extra_id.password" "{{NETWORK_BUNDLE_DIR}}/"
    echo "Generated {{CONFIG_DIR}}/client-on-network.toml (exit server via ${lan_ip})"
    echo "Bundled remote-client files into {{NETWORK_BUNDLE_DIR}}"

# ─── Client ──────────────────────────────────────────────────────────────────

# Start a gnosis_vpn-client container (CAP_NET_ADMIN, no sudo needed — see README): _client-start NAME STATE_DIR EXTRA_INDEX
_client-start name state_dir extra_index:
    #!/usr/bin/env bash
    set -euo pipefail
    # a container stopped with --rm is removed asynchronously: only a *running* one counts as "already there",
    # a dying one is waited out (otherwise a restart right after client-stop silently starts nothing)
    if [ "$(docker inspect -f '{{{{.State.Running}}}}' "{{name}}" 2>/dev/null)" = "true" ]; then
        echo "{{name}} already running — skipping start"
        exit 0
    fi
    for i in $(seq 1 30); do docker container inspect "{{name}}" > /dev/null 2>&1 || break; sleep 1; done
    mkdir -p "{{state_dir}}" "{{SUITE_OUT_DIR}}"
    blokli_url=$(cat "{{CONFIG_DIR}}/blokli_url" | sed 's/localhost/host.docker.internal/')
    extra_id_pass=$(cat "{{CONFIG_DIR}}/extra_id_{{extra_index}}.password")
    # the identity lives in the writable state dir: a newer client migrates an older keystore format in place,
    # which fails on the read-only /config mount ("Read-only file system", worker exit 71)
    cp "{{CONFIG_DIR}}/extra_id_{{extra_index}}.id" "{{state_dir}}/identity.id"
    extra_env=(); for kv in {{CLIENT_EXTRA_ENV}}; do extra_env+=(--env "${kv}"); done
    sysctl_args=(); [ -n "{{CLIENT_SYSCTL}}" ] && sysctl_args=(--sysctl "{{CLIENT_SYSCTL}}")
    docker run --detach --rm \
        --name "{{name}}" \
        --network "{{DOCKER_NETWORK}}" \
        --cap-add=NET_ADMIN \
        --device /dev/net/tun \
        --add-host=host.docker.internal:host-gateway \
        --log-opt max-size={{LOG_MAX_SIZE}} --log-opt max-file={{LOG_MAX_FILE}} \
        --sysctl net.ipv4.ping_group_range="0 2147483647" \
        "${sysctl_args[@]}" \
        --env RUST_LOG="{{CLIENT_LOG_LEVEL}}" \
        --env GNOSISVPN_CONFIG_PATH=/config/client.toml \
        --env GNOSISVPN_HOPR_BLOKLI_URL="${blokli_url}" \
        --env GNOSISVPN_HOPR_IDENTITY_FILE=/var/lib/gnosisvpn/identity.id \
        --env GNOSISVPN_HOPR_IDENTITY_PASS="${extra_id_pass}" \
        --env GNOSISVPN_HOME=/var/lib/gnosisvpn \
        --env GNOSISVPN_CLIENT_AUTOSTART={{CLIENT_AUTOSTART}} \
        "${extra_env[@]}" \
        --volume "{{CONFIG_DIR}}:/config:ro" \
        --volume "{{state_dir}}:/var/lib/gnosisvpn" \
        --volume "{{justfile_directory()}}/scripts/suite:/suite:ro" \
        --volume "{{SUITE_OUT_DIR}}:/suite-out" \
        {{CLIENT_IMAGE}} {{CLIENT_EXTRA_ARGS}}
    sleep 1
    running=$(docker inspect "{{name}}" 2>/dev/null | jq -r '.[0].State.Running // "false"')
    if [ "${running}" != "true" ]; then
        echo "Error: {{name}} failed to start" >&2
        { docker logs "{{name}}" 2>&1 || true; } >&2
        exit 1
    fi
    echo "Started {{name}} ({{CLIENT_IMAGE}}, identity extra_id_{{extra_index}})"

# Start the gnosis_vpn-client container
client-start: network-create
    just _client-start gnosis_vpn-client "{{CLIENT_STATE_DIR}}" 0

# Start a second client container on extra identity 1 (needs EXTRA_IDENTITIES=2 at cluster start)
client2-start: network-create
    just _client-start gnosis_vpn-client-2 "{{CLIENT_STATE_DIR}}-2" 1

# Start CLIENT_COUNT client containers: the primary plus extras 2..N on extra identities 1..N-1 (T22-concurrent-clients ladder)
clients-start: network-create
    #!/usr/bin/env bash
    set -euo pipefail
    just _client-start gnosis_vpn-client "{{CLIENT_STATE_DIR}}" 0
    for i in $(seq 2 {{CLIENT_COUNT}}); do
        just _client-start "gnosis_vpn-client-${i}" "{{CLIENT_STATE_DIR}}-${i}" "$((i-1))"
    done
    echo "clients running: {{CLIENT_COUNT}}"

# Stop every extra client container (the primary is left alone; use client-stop for that)
clients-stop:
    #!/usr/bin/env bash
    for i in $(seq 2 16); do
        name="gnosis_vpn-client-${i}"
        docker container inspect "${name}" >/dev/null 2>&1 || continue
        docker stop "${name}" >/dev/null 2>&1 || true
        for j in $(seq 1 30); do docker container inspect "${name}" >/dev/null 2>&1 || break; sleep 1; done
        rm -rf "{{CLIENT_STATE_DIR}}-${i}" 2>/dev/null || sudo rm -rf "{{CLIENT_STATE_DIR}}-${i}" 2>/dev/null || true
    done

# Stop the second client container
client2-stop:
    #!/usr/bin/env bash
    docker stop gnosis_vpn-client-2 2>/dev/null || true
    for i in $(seq 1 30); do docker container inspect gnosis_vpn-client-2 > /dev/null 2>&1 || break; sleep 1; done
    rm -rf "{{CLIENT_STATE_DIR}}-2" 2>/dev/null || sudo rm -rf "{{CLIENT_STATE_DIR}}-2" 2>/dev/null || true

# Stop the client, wherever it's running (container or host-native — used by down)
client-stop:
    #!/usr/bin/env bash
    docker stop gnosis_vpn-client 2>/dev/null || true
    # --rm removal is asynchronous; wait for it so a following client-start does not see a dying container
    for i in $(seq 1 30); do docker container inspect gnosis_vpn-client > /dev/null 2>&1 || break; sleep 1; done
    # only touch sudo if a host-native client is actually running, so the container-only
    # workflow (the common case) never hits a sudo prompt here. Match the host-native binary path,
    # not the bare name: container processes (a second client) are visible to pgrep too.
    native="{{GVPN_CLIENT_DIR}}/result/bin/gnosis_vpn-"
    if pgrep -f "^${native}root" > /dev/null 2>&1 || pgrep -f "^${native}worker" > /dev/null 2>&1; then
        sudo pkill -f "^${native}root"   2>/dev/null || true
        sudo pkill -f "^${native}worker" 2>/dev/null || true
    fi
    true

# Reintroduces the routing-loop risk the container was built to avoid (see README "Why the
# client runs in its own container") if gnosis_vpn-server shares the host's egress — don't run
# this alongside `client-start`, they'd collide over CLIENT_STATE_DIR and the default control socket.
# Start gnosis_vpn-client as a native host process instead of in Docker (dev/debug convenience)
client-start-on-host:
    #!/usr/bin/env bash
    set -euo pipefail
    root_bin="{{GVPN_CLIENT_DIR}}/result/bin/gnosis_vpn-root"
    worker_bin="{{GVPN_CLIENT_DIR}}/result/bin/gnosis_vpn-worker"
    if [ ! -f "${root_bin}" ] || [ ! -f "${worker_bin}" ]; then
        echo "Error: gnosis_vpn-client binaries not found at {{GVPN_CLIENT_DIR}}/result/bin/" >&2
        echo "Run 'just build-client-native' to build them first" >&2
        exit 1
    fi
    if ! id "{{CLIENT_WORKER_USER}}" > /dev/null 2>&1; then
        echo "Error: worker user '{{CLIENT_WORKER_USER}}' not found on this host." >&2
        echo "gnosis_vpn-root drops privileges to this user (by uid/gid) when spawning gnosis_vpn-worker," >&2
        echo "so it must already exist as a system account (its home directory is irrelevant — create" >&2
        echo "one via your NixOS config, or override the name via CLIENT_WORKER_USER)." >&2
        exit 1
    fi
    if pgrep -f gnosis_vpn-root > /dev/null 2>&1; then
        echo "Client already running on host — skipping start"
        exit 0
    fi
    mkdir -p "{{CLIENT_STATE_DIR}}"
    blokli_url=$(cat "{{CONFIG_DIR}}/blokli_url")
    extra_id_pass=$(cat "{{CONFIG_DIR}}/extra_id.password")
    # sudo backgrounded can't read TTY; pre-authenticate while still interactive
    sudo -v
    sudo RUST_LOG="{{CLIENT_LOG_LEVEL}}" \
        GNOSISVPN_CONFIG_PATH="{{CONFIG_DIR}}/client.toml" \
        GNOSISVPN_HOPR_BLOKLI_URL="${blokli_url}" \
        GNOSISVPN_HOPR_IDENTITY_FILE="{{CONFIG_DIR}}/extra_id.id" \
        GNOSISVPN_HOPR_IDENTITY_PASS="${extra_id_pass}" \
        GNOSISVPN_HOME="{{CLIENT_STATE_DIR}}" \
        GNOSISVPN_CLIENT_AUTOSTART=30min \
        GNOSISVPN_WORKER_USER="{{CLIENT_WORKER_USER}}" \
        GNOSISVPN_LOG_FILE="{{CLIENT_LOG_FILE}}" \
        "${root_bin}" --worker-binary "${worker_bin}" &
    echo "Client PID: $!"

# Stop the host-native client (cascades SIGTERM to the worker via gnosis_vpn-root)
client-stop-on-host:
    #!/usr/bin/env bash
    sudo pkill -f gnosis_vpn-root   2>/dev/null || true
    sudo pkill -f gnosis_vpn-worker 2>/dev/null || true
    echo "Client (host) stopped"

# Tail the host-native client's log file
client-logs-on-host:
    tail -f "{{CLIENT_LOG_FILE}}"

# Purge worker state without prompting (used by down).
# sudo: the container's entrypoint chowns this bind-mounted dir to its internal
# worker uid, which may not be removable by the host user without it.
_purge-state:
    sudo rm -rf "{{CLIENT_STATE_DIR}}"
    echo "Purged {{CLIENT_STATE_DIR}}"

# Remove all persistent worker state (identity keys, cache) from CLIENT_STATE_DIR
purge-state:
    #!/usr/bin/env bash
    set -euo pipefail
    read -r -p "Permanently delete '{{CLIENT_STATE_DIR}}'? Type 'yes' to confirm: " answer
    if [ "${answer}" != "yes" ]; then
        echo "Aborted"
        exit 1
    fi
    sudo rm -rf "{{CLIENT_STATE_DIR}}"
    echo "Purged {{CLIENT_STATE_DIR}}"

# ─── System tests ────────────────────────────────────────────────────────────

# Run gnosis_vpn-client system tests against the live local stack
system-tests:
    #!/usr/bin/env bash
    set -euo pipefail
    for artifact in client.toml extra_id.id extra_id.password extra_id.safe blokli_url; do
        if [ ! -f "{{CONFIG_DIR}}/${artifact}" ]; then
            echo "Missing {{CONFIG_DIR}}/${artifact} — run 'just gen-config' first" >&2
            exit 1
        fi
    done

    worker_binary="{{GVPN_CLIENT_DIR}}/result/bin/gnosis_vpn-worker"
    if [ ! -f "${worker_binary}" ]; then
        echo "Missing ${worker_binary} — run 'just build-client' first" >&2
        exit 1
    fi

    SYSTEM_TEST_HOPRD_ID=$(cat "{{CONFIG_DIR}}/extra_id.id") \
    SYSTEM_TEST_HOPRD_ID_PASSWORD=$(cat "{{CONFIG_DIR}}/extra_id.password") \
    SYSTEM_TEST_SAFE=$(cat "{{CONFIG_DIR}}/extra_id.safe") \
    SYSTEM_TEST_CONFIG=$(cat "{{CONFIG_DIR}}/client.toml") \
    SYSTEM_TEST_WORKER_BINARY="${worker_binary}" \
        just -d "{{GVPN_CLIENT_DIR}}" -f "{{GVPN_CLIENT_DIR}}/justfile" system-tests

# ─── End-to-end tests ────────────────────────────────────────────────────────

# Build the e2e sidecar image (node + obscura + browser harness)
build-e2e:
    docker build --tag "{{E2E_IMAGE}}" e2e

# Drive a headless browser through the tunnel for every destination (see e2e/README.md)
e2e *ARGS: build-e2e
    #!/usr/bin/env bash
    set -euo pipefail
    E2E_IMAGE="{{E2E_IMAGE}}" \
    E2E_OUT_DIR="{{E2E_OUT_DIR}}" \
    CLUSTER_SIZE="{{CLUSTER_SIZE}}" \
    SERVER_COUNT="{{SERVER_COUNT}}" \
    HOPS="{{HOPS}}" \
    GVPN_CLIENT_DIR="{{GVPN_CLIENT_DIR}}" \
    GVPN_SERVER_DIR="{{GVPN_SERVER_DIR}}" \
    HOPRD_DIR="{{HOPRD_DIR}}" \
        "{{justfile_directory()}}/e2e/run.sh" {{ARGS}}

# Mirrors how `system-tests` owns its daemon instead of attaching to a pre-running one.
# Needs sudo (WireGuard + routing table) and a pre-existing worker user — macOS has no
# useradd, so unlike the Linux-only system-tests recipe this does not create one.
# Full tunnel: while it runs, this machine's traffic egresses through the exit under test.
# Run the e2e browser suite against a real network (rotsee etc), starting our own client
e2e-on-network *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    : "${E2E_ROOT_BINARY:?E2E_ROOT_BINARY must point at a gnosis_vpn-root binary for this host}"
    : "${E2E_WORKER_BINARY:?E2E_WORKER_BINARY must point at a gnosis_vpn-worker binary for this host}"
    : "${E2E_CONFIG:?E2E_CONFIG must point at a client config (e.g. rotsee.toml)}"
    : "${E2E_IDENTITY_FILE:?E2E_IDENTITY_FILE must point at the HOPR identity file}"
    : "${E2E_IDENTITY_PASS_FILE:?E2E_IDENTITY_PASS_FILE must point at a file holding the identity password}"
    : "${E2E_BLOKLI_URL:?E2E_BLOKLI_URL must be set (e.g. https://blokli.rotsee.hoprnet.link)}"

    # gnosis_vpn-worker runs unprivileged, so its binary/identity/config must be readable by
    # E2E_WORKER_USER — a path under someone's home usually is not. Stage into a world-readable dir.
    stage="${E2E_STAGE_DIR:-/tmp/gnosis_vpn-e2e-stage}"
    rm -rf "${stage}"; mkdir -p "${stage}"
    cp "${E2E_ROOT_BINARY}"   "${stage}/gnosis_vpn-root"
    cp "${E2E_WORKER_BINARY}" "${stage}/gnosis_vpn-worker"
    cp "${E2E_CONFIG}"        "${stage}/config.toml"
    cp "${E2E_IDENTITY_FILE}" "${stage}/identity.id"
    # Default to the .safe sitting next to the identity, as gnosis_vpn lays it out
    safe_file="${E2E_IDENTITY_SAFE_FILE:-${E2E_IDENTITY_FILE%.id}.safe}"
    if [ -f "${safe_file}" ]; then
        cp "${safe_file}" "${stage}/identity.safe"
    else
        echo "WARNING: no .safe found next to the identity (${safe_file}); the client will try to onboard a NEW safe" >&2
    fi
    chmod -R a+rX "${stage}"
    chmod a+rx "${stage}/gnosis_vpn-root" "${stage}/gnosis_vpn-worker"

    E2E_IMAGE="{{E2E_IMAGE}}" \
    E2E_OUT_DIR="{{E2E_OUT_DIR}}" \
    CLIENT_MODE=spawn \
    WORKER_USER="${E2E_WORKER_USER:-gnosisvpn-dev}" \
    GVPN_ROOT_BIN="${stage}/gnosis_vpn-root" \
    GVPN_WORKER_BIN="${stage}/gnosis_vpn-worker" \
    GVPN_CONFIG="${stage}/config.toml" \
    GVPN_IDENTITY_FILE="${stage}/identity.id" \
    GVPN_IDENTITY_PASS="$(cat "${E2E_IDENTITY_PASS_FILE}")" \
    GVPN_IDENTITY_SAFE="$([ -f "${stage}/identity.safe" ] && echo "${stage}/identity.safe" || echo "")" \
    GVPN_BLOKLI_URL="${E2E_BLOKLI_URL}" \
    PING_TARGETS="${PING_TARGETS:-1.1.1.1,10.128.0.1}" \
        "{{justfile_directory()}}/e2e/run.sh" {{ARGS}}

# ─── Traffic target ──────────────────────────────────────────────────────────

# Start the in-cluster traffic target: on the default bridge (reached through the exit) and on DOCKER_NETWORK (reached directly for baselines)
target-start: network-create
    #!/usr/bin/env bash
    set -euo pipefail
    if docker container inspect "{{TARGET_NAME}}" > /dev/null 2>&1; then
        echo "{{TARGET_NAME}} already exists — skipping start"
        exit 0
    fi
    docker image inspect "{{TARGET_IMAGE}}" > /dev/null 2>&1 || just build-target
    docker network inspect "{{TARGET_NETWORK}}" > /dev/null 2>&1 \
        || docker network create --subnet "{{TARGET_SUBNET}}" "{{TARGET_NETWORK}}" > /dev/null
    docker run --detach --rm --name "{{TARGET_NAME}}" \
        --network "{{TARGET_NETWORK}}" \
        --log-opt max-size={{LOG_MAX_SIZE}} --log-opt max-file={{LOG_MAX_FILE}} \
        "{{TARGET_IMAGE}}" > /dev/null
    docker network connect "{{DOCKER_NETWORK}}" "{{TARGET_NAME}}"
    via=$(docker inspect "{{TARGET_NAME}}" | jq -r '.[0].NetworkSettings.Networks["{{TARGET_NETWORK}}"].IPAddress')
    direct=$(docker inspect "{{TARGET_NAME}}" | jq -r '.[0].NetworkSettings.Networks["{{DOCKER_NETWORK}}"].IPAddress')
    # every running exit server joins the target network and NATs tunnel clients into it
    for i in $(seq 0 $(({{SERVER_COUNT}} - 1))); do
        srv="gnosis_vpn-server-${i}"
        docker container inspect "${srv}" > /dev/null 2>&1 || continue
        docker network connect "{{TARGET_NETWORK}}" "${srv}" 2>/dev/null || true
        docker exec "${srv}" sh -c 'iface=$(ip -o -4 addr show to {{TARGET_SUBNET}} | awk "{print \$2}" | head -1); [ -n "$iface" ] && { iptables -t nat -C POSTROUTING -s 10.129.0.0/24 -o "$iface" -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -s 10.129.0.0/24 -o "$iface" -j MASQUERADE; }'
    done
    echo "Started {{TARGET_NAME}}: via exit ${via}  direct ${direct}  (http :8899, udp :8901 echo, :8902 stream, :8903 call)"

# Stop the traffic target
target-stop:
    docker stop "{{TARGET_NAME}}" 2>/dev/null || true
    docker network rm "{{TARGET_NETWORK}}" 2>/dev/null || true

# ─── Regression suite (docs/regression-catalogue.md) ─────────────────────────

# Environment every suite script needs, derived from the justfile variables
_suite-env:
    #!/usr/bin/env bash
    cat <<ENV
    export TESTENV_DIR="{{justfile_directory()}}" LOCALCLUSTER_BIN="{{LOCALCLUSTER_BIN}}" HOPRD_BIN="{{HOPRD_BIN}}" HOPRD_DIR="{{HOPRD_DIR}}"
    export DATA_DIR="{{DATA_DIR}}" CONFIG_DIR="{{CONFIG_DIR}}" CLUSTER_SIZE="{{CLUSTER_SIZE}}" DOCKER_NETWORK="{{DOCKER_NETWORK}}"
    export CLIENT_IMAGE="{{CLIENT_IMAGE}}" SERVER_IMAGE="{{SERVER_IMAGE}}" TARGET_NAME="{{TARGET_NAME}}" TARGET_NETWORK="{{TARGET_NETWORK}}" SUITE_OUT_DIR="{{SUITE_OUT_DIR}}"
    export CLIENT_STATE_DIR="{{CLIENT_STATE_DIR}}" CLIENT_EXTRA_ARGS="{{CLIENT_EXTRA_ARGS}}" CLIENT_EXTRA_ENV="{{CLIENT_EXTRA_ENV}}" CLIENT_SYSCTL="{{CLIENT_SYSCTL}}"
    export CLUSTER_ENV="{{CLUSTER_ENV}}" CLUSTER_LATENCY="{{CLUSTER_LATENCY}}" GVPN_CLIENT_DIR="{{GVPN_CLIENT_DIR}}" GVPN_SERVER_DIR="{{GVPN_SERVER_DIR}}"
    ENV

# Run one catalogue test against the live stack (t01 is not forced in front): just test t04 [--fast] [--knob T04_REPS=1]
test name *args:
    #!/usr/bin/env bash
    set -euo pipefail
    eval "$(just _suite-env)"
    exec python3 "{{justfile_directory()}}/scripts/suite/run.py" --only "{{name}}" --no-preconditions {{args}}

# Run the regression suite (one run, every test; --fast, --very-fast, --only, --skip, --knob) against the live stack; results in SUITE_OUT_DIR/<run-id>/
suite *args:
    #!/usr/bin/env bash
    set -euo pipefail
    eval "$(just _suite-env)"
    exec python3 "{{justfile_directory()}}/scripts/suite/run.py" {{args}}

# Offline self-tests: the suite library, the target's own tests, and every probe against every target service on loopback (no stack needed)
suite-selftest: target-test
    python3 -m pytest "{{justfile_directory()}}/scripts/suite/selftest" -q

# The traffic target's own tests (docker/target/tests), on loopback
target-test:
    cd "{{justfile_directory()}}/docker/target" && python3 -m pytest -q

# The daily battery: build the sibling checkouts AS THEY ARE (nothing here pulls or pins upstream tags; the timer's
# job is to check them out at the versions to test first), bring the stack up, run the suite (--fast by default; pass
# e.g. "--very-fast" or "--run-id nightly-$(date +%F)"), take it down. Exit code is the suite's.
nightly *args="--fast":
    #!/usr/bin/env bash
    set -uo pipefail
    just build && just up-nobuild && sleep 180 || { echo "stack failed to come up" >&2; exit 2; }
    just suite {{args}}; rc=$?
    just down >/dev/null 2>&1 || true
    exit $rc

# Bring the stack up without building (pre-built binaries and images), including target and client
up-nobuild: metrics-start cluster-start cluster-wait server-start gen-config target-start clients-start
    @just summary

# Restart only the cluster (new HOPRD_BIN / CLUSTER_ENV / CLUSTER_LATENCY), regenerate config, restart the client
cluster-restart: client-stop cluster-stop cluster-start cluster-wait gen-config client-start

# Run the suite across version/config cells from a cells file (see scripts/suite/matrix.py --help)
matrix cells *args:
    #!/usr/bin/env bash
    set -euo pipefail
    eval "$(just _suite-env)"
    exec python3 "{{justfile_directory()}}/scripts/suite/matrix.py" "{{cells}}" {{args}}

# ─── Scripts ─────────────────────────────────────────────────────────────────

# validate connectivity on an already-connected tunnel (see scripts/README.md)
smoke-test *args:
    ./scripts/vpn-smoke-test.sh {{ args }}

# cycle every destination until account funding drains, recording results (see scripts/README.md)
drain-tour *args:
    ./scripts/vpn-drain-tour.sh {{ args }}

# render a vpn-drain-tour run directory into a self-contained report.html
drain-report *args:
    ./scripts/vpn-drain-report.sh {{ args }}

# sample a WireGuard interface's byte counters, logging per-window totals to CSV
wg-traffic *args:
    ./scripts/wg-traffic.sh {{ args }}

# run the offline bats suite for scripts/ (no network, uses fakes)
test-scripts:
    bats --print-output-on-failure scripts/tests

# ─── Metrics ─────────────────────────────────────────────────────────────────

# Start otelcol (OTLP HTTP on 127.0.0.1:4318) and VictoriaMetrics (PromQL UI on :8428)
metrics-start:
    #!/usr/bin/env bash
    set -euo pipefail
    configs_dir="{{justfile_directory()}}/configs"

    otelcol_running=$(pgrep -f "otelcol --config" 2>/dev/null || true)
    if [ -n "${otelcol_running}" ]; then
        echo "Metrics found (PID ${otelcol_running}) — skipping start"
        echo "  OTLP HTTP: 127.0.0.1:4318 | PromQL UI: http://localhost:8428"
        exit 0
    fi

    mkdir -p "{{METRICS_DATA_DIR}}"

    setsid nohup otelcol --config "${configs_dir}/otelcol.yaml" > /tmp/hopr-otelcol.log 2>&1 &
    setsid nohup victoria-metrics \
        -storageDataPath "{{METRICS_DATA_DIR}}" \
        -httpListenAddr "127.0.0.1:8428" \
        > /tmp/hopr-victoriametrics.log 2>&1 &

    echo "Started metrics — OTLP HTTP: 127.0.0.1:4318 | PromQL UI: http://localhost:8428"

# Stop otelcol and VictoriaMetrics
metrics-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    pkill -f "otelcol --config" 2>/dev/null || true
    pkill -f "victoria-metrics" 2>/dev/null || true
    echo "Metrics stopped"

# ─── Composite ───────────────────────────────────────────────────────────────

# Bring the full stack up, including the client container
up: build metrics-start cluster-start cluster-wait server-start gen-config client-start
    @just summary

# See the caveat on client-start-on-host before using this instead of `up`
# Bring the full stack up with the client running natively on the host instead of in Docker
up-client-on-host: build-cluster build-server build-client-native metrics-start cluster-start-on-host cluster-wait server-start gen-config client-start-on-host
    @just summary-host-client

# Bring up a cluster + exit server reachable from another machine on the LAN, and generate its client config
up-on-network: build-cluster build-server metrics-start cluster-start-on-network cluster-wait server-start gen-config-on-network
    @just summary-on-network

# Print how to control the running client and component versions
summary:
    #!/usr/bin/env bash
    set -uo pipefail
    echo ""
    echo "── Gnosis VPN test stack ──────────────────────────────────────"
    echo ""
    echo "Control the client:"
    echo "  docker exec -it gnosis_vpn-client gnosis_vpn-ctl status"
    echo "  docker exec -it gnosis_vpn-client gnosis_vpn-ctl connect <destination-id>"
    echo "  docker logs -f gnosis_vpn-client"
    echo ""
    echo "Component versions:"
    just _component-version "gnosis_vpn-client" "{{GVPN_CLIENT_DIR}}"
    just _component-version "gnosis_vpn-server" "{{GVPN_SERVER_DIR}}"
    just _component-version "hoprd"             "{{HOPRD_DIR}}"
    echo ""
    echo "Metrics — OTLP HTTP: 127.0.0.1:4318 | PromQL UI: http://localhost:8428"
    echo "─────────────────────────────────────────────────────────────────"

# Print how to control the host-native client and component versions
summary-host-client:
    #!/usr/bin/env bash
    set -uo pipefail
    echo ""
    echo "── Gnosis VPN test stack (client on host) ─────────────────────"
    echo ""
    echo "Control the client:"
    echo "  {{GVPN_CLIENT_DIR}}/result/bin/gnosis_vpn-ctl status"
    echo "  {{GVPN_CLIENT_DIR}}/result/bin/gnosis_vpn-ctl connect <destination-id>"
    echo "  just client-logs-on-host"
    echo ""
    echo "Component versions:"
    just _component-version "gnosis_vpn-client" "{{GVPN_CLIENT_DIR}}"
    just _component-version "gnosis_vpn-server" "{{GVPN_SERVER_DIR}}"
    just _component-version "hoprd"             "{{HOPRD_DIR}}"
    echo ""
    echo "Metrics — OTLP HTTP: 127.0.0.1:4318 | PromQL UI: http://localhost:8428"
    echo "─────────────────────────────────────────────────────────────────"

# Print what to copy/run on the other machine, the required firewall ports, and component versions
summary-on-network:
    #!/usr/bin/env bash
    set -uo pipefail
    lan_ip=$(just _lan-ip)
    remote_user=$(whoami)
    blokli_url=$(cat "{{CONFIG_DIR}}/blokli_url-on-network")
    bundle_dir="/tmp/gnosis_vpn-on-network"
    worker_home="/home/{{CLIENT_WORKER_USER}}"
    echo ""
    echo "── Gnosis VPN test stack (reachable on the LAN at ${lan_ip}) ───"
    echo ""
    echo "On the other machine, from a gnosis_vpn-client checkout built with"
    echo "'cargo build --release':"
    echo "  1. Pull the bundled config/identity files from this host, into a"
    echo "     world-readable location — gnosis_vpn-worker reads the identity"
    echo "     file as an unprivileged user, so it can't sit under your home dir:"
    echo "       rsync -avz ${remote_user}@${lan_ip}:{{NETWORK_BUNDLE_DIR}}/ ${bundle_dir}/"
    echo "  2. Make sure worker user '{{CLIENT_WORKER_USER}}' exists on that machine too"
    echo "     (gnosis_vpn-root drops privileges to it when spawning gnosis_vpn-worker)"
    echo "  3. The worker binary has the same problem as the identity file above —"
    echo "     target/release sits under your home dir, unreachable for the worker"
    echo "     user (a nix build wouldn't need this, its result lives in the"
    echo "     world-readable /nix/store). Copy it out and hand it to the worker user:"
    echo "       sudo rm -f ${worker_home}/gnosis_vpn-worker"
    echo "       sudo cp ./target/release/gnosis_vpn-worker ${worker_home}/"
    echo "       sudo chown {{CLIENT_WORKER_USER}}:gnosisvpn ${worker_home}/gnosis_vpn-worker"
    echo "  4. Run:"
    echo "       sudo RUST_LOG=info \\"
    echo "       ./target/release/gnosis_vpn-root \\"
    echo "         --config-path ${bundle_dir}/client-on-network.toml \\"
    echo "         --hopr-blokli-url \"${blokli_url}\" \\"
    echo "         --hopr-identity-file ${bundle_dir}/extra_id.id \\"
    echo "         --hopr-identity-pass \"\$(cat ${bundle_dir}/extra_id.password)\" \\"
    echo "         --client-autostart 30min \\"
    echo "         --worker-user {{CLIENT_WORKER_USER}} \\"
    echo "         --state-home ${worker_home} \\"
    echo "         --worker-binary ${worker_home}/gnosis_vpn-worker"
    echo ""
    echo "Make sure this host's firewall allows inbound from the other machine on:"
    echo "  UDP 9000..$(({{CLUSTER_SIZE}} - 1 + 9000))   (HOPR P2P)"
    echo "  TCP 8080                                     (Blokli)"
    echo "  TCP 8000..$(({{SERVER_COUNT}} - 1 + 8000))   (VPN server API)"
    echo "  UDP 51821..$(({{SERVER_COUNT}} - 1 + 51821)) (VPN server WireGuard)"
    echo ""
    echo "Component versions:"
    just _component-version "gnosis_vpn-server" "{{GVPN_SERVER_DIR}}"
    just _component-version "hoprd"             "{{HOPRD_DIR}}"
    echo ""
    echo "Metrics (on this host) — OTLP HTTP: 127.0.0.1:4318 | PromQL UI: http://localhost:8428"
    echo "─────────────────────────────────────────────────────────────────"

# Resolve the LAN-reachable IP: LAN_IP override, or auto-detect via default route
_lan-ip:
    #!/usr/bin/env bash
    set -euo pipefail
    # Downstream recipes splice this into host:port strings and firewall rules, so it must be
    # a plain IPv4 dotted-quad — a hostname or IPv6 address would silently produce invalid targets.
    ipv4_pattern='^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'
    if [ -n "{{LAN_IP}}" ]; then
        if ! [[ "{{LAN_IP}}" =~ ${ipv4_pattern} ]]; then
            echo "Error: LAN_IP='{{LAN_IP}}' is not an IPv4 dotted-quad (hostnames/IPv6 aren't supported)" >&2
            exit 1
        fi
        echo "{{LAN_IP}}"
        exit 0
    fi
    lan_ip=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p')
    if [ -z "${lan_ip}" ]; then
        echo "Error: could not auto-detect a LAN IP (no default route?) — set LAN_IP explicitly" >&2
        exit 1
    fi
    echo "${lan_ip}"

# The P2P host the currently running cluster (if any) was started with, or empty if not running.
# hoprd-localcluster's status JSON reports each node's dial address (host:port) from the moment
# it's created — deterministic from --p2p-host, so this reflects the real flag even before any
# node is ready.
_cluster-p2p-host:
    #!/usr/bin/env bash
    set -euo pipefail
    "{{LOCALCLUSTER_BIN}}" status --data-dir "{{DATA_DIR}}" 2>/dev/null \
        | jq -r '.nodes[0].p2p // empty' | sed -n 's/:[0-9]*$//p'

# Print <name>'s checked-out branch and commit, plus tag if HEAD is exactly tagged
_component-version name dir:
    #!/usr/bin/env bash
    set -uo pipefail
    if [ ! -d "{{dir}}/.git" ]; then
        echo "  {{name}}: {{dir}} (not a git checkout)"
        exit 0
    fi
    commit=$(git -C "{{dir}}" rev-parse --short HEAD 2>/dev/null) || { echo "  {{name}}: unable to resolve commit"; exit 0; }
    branch=$(git -C "{{dir}}" symbolic-ref --short -q HEAD || echo "detached")
    tag=$(git -C "{{dir}}" describe --tags --exact-match 2>/dev/null || true)
    dirty=""
    [ -n "$(git -C "{{dir}}" status --porcelain 2>/dev/null)" ] && dirty=" (dirty)"
    if [ -n "${tag}" ]; then
        echo "  {{name}}: ${tag} (${branch}, ${commit})${dirty}"
    else
        echo "  {{name}}: ${branch} (${commit})${dirty}"
    fi

# Tear the full stack down and purge client state (cluster always restarts with new identities)
down: client-stop clients-stop client2-stop target-stop server-stop cluster-stop metrics-stop _purge-state

# Remove all generated configs, data, logs, chain container, and nix build results
clean:
    rm -rf "{{CONFIG_DIR}}" "{{DATA_DIR}}" "{{METRICS_DATA_DIR}}"
    sudo rm -rf "{{CLIENT_STATE_DIR}}"
    sudo rm -f /tmp/hopr-otelcol.log /tmp/hopr-victoriametrics.log
    docker rm -f hopr-chain 2>/dev/null || true
    just network-remove
    rm -f "{{HOPRD_DIR}}/result-hoprd" "{{HOPRD_DIR}}/result-localcluster" "{{GVPN_CLIENT_DIR}}/result"
    echo "Clean done"

# Tear the full stack down and wipe all state
reset: down clean

# Full development setup: build, bring the whole stack up including the client container
development-setup: up

# Tail all cluster node logs and the client container's logs
logs:
    #!/usr/bin/env bash
    tail -f "{{DATA_DIR}}/logs/"*.log &
    docker logs -f gnosis_vpn-client

# Tail only cluster node logs
node-logs:
    tail -f "{{DATA_DIR}}/logs/"hoprd_*.log
