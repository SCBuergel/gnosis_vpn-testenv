# Every `:=` variable below reaches scripts/testenv/* as an environment variable; recipe
# parameters travel as argv instead, since `set export` would join a `*ARGS` variadic into one word.
set export := true

# Paths to sibling repos — override via env in CI
HOPRD_DIR       := env_var_or_default("HOPRD_DIR",       "../hoprd")
GVPN_SERVER_DIR := env_var_or_default("GVPN_SERVER_DIR", "../gnosis_vpn-server")
GVPN_CLIENT_DIR := env_var_or_default("GVPN_CLIENT_DIR", "../gnosis_vpn-client")

# Localcluster settings
CLUSTER_SIZE := env_var_or_default("CLUSTER_SIZE", "3")
# Binaries the cluster runs. Defaults are the nix out-links of build-cluster; override to run a
# stock release binary, a cargo build, or a different hoprd version per cell (regression suite T25-knob-ab/T26-version-matrix).
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

# The PIX deposit pool both ends settle through when PIX is on: `test` (visible secp256k1
# transfers) or `curvy` (anonymous, through the Curvy deployment `curvy-stack-up` runs next to the
# cluster). It picks the hoprd binary and the client image together, because the curve each pool
# settles to is network-wide and never negotiated — see the note on build-cluster. Set by `up-curvy`.
# Empty rather than "test" so pix/run.sh still reaches its detect-the-pool-from-the-client-image path.
CLUSTER_PIX_POOL := env_var_or_default("CLUSTER_PIX_POOL", "")
HOPRD_PACKAGE    := if CLUSTER_PIX_POOL == "curvy" { "binary-hoprd-pix-curvy-x86_64-linux" } else { "binary-hoprd-pix-test-x86_64-linux" }
HOPRD_RESULT     := if CLUSTER_PIX_POOL == "curvy" { "result-hoprd-pix-curvy" } else { "result-hoprd" }
CLIENT_IMAGE     := env_var_or_default("CLIENT_IMAGE", if CLUSTER_PIX_POOL == "curvy" { "gnosis_vpn-client:pix-curvy" } else { "gnosis_vpn-client" })   # env override per cell (T26-version-matrix); the pix-curvy tag under the Curvy pool
CLIENT_BUILD     := if CLUSTER_PIX_POOL == "curvy" { "docker-build-pix-curvy" } else { "docker-build" }

# The Curvy stack is published on the Docker bridge's gateway, so the host-native nodes and the
# client container reach it at the same address. Its gateway (relayer, indexer) moves off 3000,
# which is node-0's API port; Blokli stays on 8080, where the cluster's own chain would be.
CURVY_GATEWAY_PORT := env_var_or_default("CURVY_GATEWAY_PORT", "3900")
CURVY_STACK_ENV    := CONFIG_DIR + "/curvy-stack.env"

# The two cluster binaries. Default to what `build-cluster` produces; override to run against
# binaries built some other way (e.g. `cargo build --release -p hoprd --features strategy-pix-test`
# plus `-p hoprd-localcluster` in a checkout of another commit). Override them together: the
# localcluster writes the node configs the hoprd binary then has to accept.
HOPRD_BIN        := env_var_or_default("HOPRD_BIN",        HOPRD_DIR + "/" + HOPRD_RESULT + "/bin/hoprd")
LOCALCLUSTER_BIN := env_var_or_default("LOCALCLUSTER_BIN", HOPRD_DIR + "/result-localcluster/bin/hoprd-localcluster")

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
# Tools sidecar per client (curl, ping, ip, python probes) in the client's network namespace: the client image stays vanilla
TOOLS_IMAGE    := env_var_or_default("TOOLS_IMAGE", "gnosis_vpn-suite-tools")
TARGET_NAME    := env_var_or_default("TARGET_NAME", "gnosis_vpn-target")
TARGET_NETWORK := env_var_or_default("TARGET_NETWORK", "gnosis-vpn-target")
TARGET_SUBNET  := env_var_or_default("TARGET_SUBNET", "198.18.0.0/24")
SUITE_OUT_DIR := env_var_or_default("SUITE_OUT_DIR", "/tmp/gnosis_vpn-testenv-suite")

# Override for the LAN IP auto-detection (multi-NIC hosts, or when the default route is wrong)
LAN_IP := env_var_or_default("LAN_IP", "")

# Data directory for VictoriaMetrics on-disk storage
METRICS_DATA_DIR := env_var_or_default("METRICS_DATA_DIR", "/tmp/hopr-metrics-data")

# Session hop count for destinations (0 = direct, 1+ = via relays)
HOPS := env_var_or_default("HOPS", "1")

# Non-empty starts the localcluster with `--enable-pix` and makes `gen-config` emit a PIX-enabled
# client config sized to match it. One switch for both ends, because they only work in agreement —
# see templates/pix-on.toml.tpl. Set by `up-pix`; `system-test-pix` needs it.
CLUSTER_ENABLE_PIX := env_var_or_default("CLUSTER_ENABLE_PIX", "")

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

# System-test run: worker user (created by the recipe if missing), its state home (wiped per run),
# and the runner's RUST_LOG
SYSTEM_TEST_WORKER_USER := env_var_or_default("SYSTEM_TEST_WORKER_USER", "gnosisvpn")
SYSTEM_TEST_STATE_DIR   := env_var_or_default("SYSTEM_TEST_STATE_DIR",   "/tmp/gnosis_vpn-testenv-system-tests")
SYSTEM_TEST_LOG_LEVEL   := env_var_or_default("SYSTEM_TEST_LOG_LEVEL",   "info,gnosis_vpn_root=debug,gnosis_vpn_lib=debug,gnosis_vpn_worker=debug")

# Generated config output dir
CONFIG_DIR    := env_var_or_default("CONFIG_DIR", "/tmp/gnosis_vpn-testenv")
TEMPLATES_DIR := justfile_directory() + "/templates"
REPO_DIR      := justfile_directory()

# Files a remote client needs, bundled together for up-on-network (see gen-config-on-network)
NETWORK_BUNDLE_DIR := CONFIG_DIR + "/on-network"

# List available recipes
default:
    @just --list

# ─── Build ───────────────────────────────────────────────────────────────────

# The node binary is the `pix-test` variant, not the default `binary-hoprd`. gnosis_vpn-client
# builds edgli with `pix-test`, i.e. `hopr-lib/pix-secp256k1`, and turns PIX on for the main
# tunnel session by default — while `binary-hoprd` takes hopr-lib's default, `pix-bjj`. The curve
# is a network-wide invariant that nothing negotiates, so a bjj Exit refuses every session this
# client opens with `UnacceptablePixParams` ("refusing a client offering a PIX curve suite this
# node was not built for" in the node log) and no destination ever connects. `hoprd-localcluster`
# is already built against hoprd's `strategy-pix-test`, so only the node binary was mismatched.
# Build hoprd and hoprd-localcluster binaries via nix
# With CLUSTER_PIX_POOL=curvy the node is the `pix-curvy` variant instead, and the client is built
# with edgli's `pix-curvy` to match.
build-cluster:
    nix build -L --out-link {{HOPRD_DIR}}/{{HOPRD_RESULT}} {{HOPRD_DIR}}#{{HOPRD_PACKAGE}}
    nix build -L --out-link {{HOPRD_DIR}}/result-localcluster {{HOPRD_DIR}}#binary-hoprd-localcluster

# Build gnosis_vpn-server Docker image
build-server:
    cd {{GVPN_SERVER_DIR}} && just docker-build

# Build gnosis_vpn-client Docker image (the `pix-curvy` variant under CLUSTER_PIX_POOL=curvy)
build-client:
    cd {{GVPN_CLIENT_DIR}} && just {{CLIENT_BUILD}}

# Build gnosis_vpn-client binaries only (no Docker image) — for the host-native client
build-client-native:
    cd {{GVPN_CLIENT_DIR}} && just build

# Build all components
build: build-cluster build-server build-client

# Build the in-cluster traffic target image (sized HTTP target, UDP echo, stream server, call server)
build-target:
    docker build -q -t "{{TARGET_IMAGE}}" "{{justfile_directory()}}/docker/target" && echo "built {{TARGET_IMAGE}}"

# Build the suite's tools sidecar image (curl, ping, iproute2, python for the probes); one sidecar runs per client
build-tools:
    docker build -q -t "{{TOOLS_IMAGE}}" "{{justfile_directory()}}/docker/suite-tools" && echo "built {{TOOLS_IMAGE}}"

# ─── Networking ──────────────────────────────────────────────────────────────

# Create the fixed-subnet Docker network joining the client container to the host-native localcluster
network-create:
    @scripts/testenv/network.sh create

# Remove the Docker network
network-remove:
    docker network rm "{{DOCKER_NETWORK}}" 2>/dev/null || true

# ─── Curvy stack ─────────────────────────────────────────────────────────────

# Bring up the Curvy deployment a `curvy` pool settles through: chain + Blokli, relayer, indexer,
# batch prover and gateway, from the release pinned in hoprd's localcluster/curvy. hoprd's launcher
# does the work (`--stack-only`); what it hands back is the environment the nodes and the client need.
curvy-stack-up: network-create
    @scripts/testenv/curvy.sh up

# Tear the Curvy stack down (no-op when it is not up)
curvy-stack-down:
    @scripts/testenv/curvy.sh down

# ─── Localcluster ────────────────────────────────────────────────────────────

# Start localcluster (--extra-identities 1 pre-funds the client identity; P2P binds to the Docker gateway IP)
cluster-start: network-create
    @scripts/testenv/cluster.sh start {{DOCKER_NETWORK_GATEWAY}}

# Start localcluster for a host-native client (see up-client-on-host); P2P binds to loopback instead of the Docker gateway
cluster-start-on-host:
    @scripts/testenv/cluster.sh start 127.0.0.1

# Start localcluster reachable from other machines on the LAN (see up-on-network); P2P binds/announces LAN_IP
cluster-start-on-network:
    @scripts/testenv/cluster.sh start-on-network

# Poll until cluster reaches state=running
cluster-wait:
    @scripts/testenv/cluster.sh wait

# Print live cluster status as JSON
cluster-status:
    @scripts/testenv/cluster.sh status

# Stop localcluster
cluster-stop:
    @scripts/testenv/cluster.sh stop

# ─── VPN Servers ─────────────────────────────────────────────────────────────

# Start SERVER_COUNT gnosis_vpn-server containers (server-i: WireGuard 51821+i/udp, API 8000+i)
server-start:
    @scripts/testenv/server.sh start

# Stop all VPN server containers
server-stop:
    @scripts/testenv/server.sh stop

# ─── Config generation ───────────────────────────────────────────────────────

# Derive client config and system-test artifacts from live cluster status
gen-config:
    @scripts/testenv/config.sh gen

# Derive a LAN-reachable client config + blokli URL for a client running on another machine (see up-on-network)
gen-config-on-network: gen-config
    @scripts/testenv/config.sh gen-on-network

# ─── Client ──────────────────────────────────────────────────────────────────

# Start the gnosis_vpn-client container (CAP_NET_ADMIN, no sudo needed — see README)
client-start: network-create
    @scripts/testenv/client.sh start

# Start a second client container on extra identity 1 (needs EXTRA_IDENTITIES=2 at cluster start)
client2-start: network-create
    @scripts/testenv/client.sh start gnosis_vpn-client-2 "{{CLIENT_STATE_DIR}}-2" 1

# Start CLIENT_COUNT client containers: the primary plus extras 2..N on extra identities 1..N-1 (T22-concurrent-clients ladder)
clients-start: network-create
    @scripts/testenv/client.sh clients-start

# Stop every extra client container (the primary is left alone; use client-stop for that)
clients-stop:
    @scripts/testenv/client.sh clients-stop

# Stop the second client container
client2-stop:
    @scripts/testenv/client.sh stop-extra gnosis_vpn-client-2 "{{CLIENT_STATE_DIR}}-2"

# Stop the client, wherever it's running (container or host-native — used by down)
client-stop:
    @scripts/testenv/client.sh stop

# Reintroduces the routing-loop risk the container was built to avoid (see README "Why the
# client runs in its own container") if gnosis_vpn-server shares the host's egress — don't run
# this alongside `client-start`, they'd collide over CLIENT_STATE_DIR and the default control socket.
# Start gnosis_vpn-client as a native host process instead of in Docker (dev/debug convenience)
client-start-on-host:
    @scripts/testenv/client.sh start-on-host

# Stop the host-native client (cascades SIGTERM to the worker via gnosis_vpn-root)
client-stop-on-host:
    @scripts/testenv/client.sh stop-on-host

# Tail the host-native client's log file
client-logs-on-host:
    tail -f "{{CLIENT_LOG_FILE}}"

# Remove all persistent worker state (identity keys, cache) from CLIENT_STATE_DIR
purge-state:
    @scripts/testenv/client.sh purge-state-interactive

# ─── System tests ────────────────────────────────────────────────────────────

# Runs the client's system-test binary directly instead of delegating to gnosis_vpn-client's own
# `system-tests` recipe: that recipe targets a *deployed* network, picking config and Blokli URL
# from a checked-in `gnosis_vpn-system_tests/networks/<name>/` fixture selected by
# SYSTEM_TEST_NETWORK. A localcluster has neither — its destinations, identity and Blokli URL are
# generated fresh by `gen-config` on every run — so the artifacts and the worker-user setup are
# staged here. Everything the runner itself needs is the same; only where it comes from differs.
# Full tunnel: while it runs, this machine's traffic egresses through the exit under test.
# Run gnosis_vpn-client system tests against the live local stack
system-tests *ARGS:
    @scripts/testenv/system-tests.sh {{ARGS}}

# Drive a full PIX deposit → key recovery → sweep cycle and assert the exit's income (see pix/run.sh)
system-test-pix *ARGS:
    @CLIENT_CONTAINER=gnosis_vpn-client pix/run.sh {{ARGS}}

# ─── End-to-end tests ────────────────────────────────────────────────────────

# Build the e2e sidecar image (node + obscura + browser harness)
build-e2e:
    docker build --tag "{{E2E_IMAGE}}" e2e

# Drive a headless browser through the tunnel for every destination (see e2e/README.md)
e2e *ARGS: build-e2e
    @e2e/run.sh {{ARGS}}

# Mirrors how `system-tests` owns its daemon instead of attaching to a pre-running one.
# Needs sudo (WireGuard + routing table) and a pre-existing worker user — macOS has no
# useradd, so unlike the Linux-only system-tests recipe this does not create one.
# Full tunnel: while it runs, this machine's traffic egresses through the exit under test.
# Run the e2e browser suite against a real network (rotsee etc), starting our own client
e2e-on-network *ARGS:
    @scripts/testenv/e2e-on-network.sh {{ARGS}}

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
    cd "{{justfile_directory()}}/tests" && exec python3 -m pytest regression --only "{{name}}" --no-preconditions {{args}}

# Run the regression suite (one run, every test; --fast, --very-fast, --only, --skip, --group, --knob) against the live stack;
# plain pytest under tests/ (conftest.py holds every option); results in SUITE_OUT_DIR/<run-id>/
suite *args:
    #!/usr/bin/env bash
    set -euo pipefail
    eval "$(just _suite-env)"
    cd "{{justfile_directory()}}/tests" && exec python3 -m pytest regression {{args}}

# Offline self-tests: the suite library, the target's own tests, and every probe against every target service on loopback (no stack needed)
suite-selftest: target-test
    python3 -m pytest "{{justfile_directory()}}/tests/selftest" -q

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

# Run the suite across version/config cells from a cells file (see tests/matrix.py --help)
matrix cells *args:
    #!/usr/bin/env bash
    set -euo pipefail
    eval "$(just _suite-env)"
    exec python3 "{{justfile_directory()}}/tests/matrix.py" "{{cells}}" {{args}}

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
    @scripts/testenv/metrics.sh start

# Stop otelcol and VictoriaMetrics
metrics-stop:
    @scripts/testenv/metrics.sh stop

# ─── Composite ───────────────────────────────────────────────────────────────

# Bring the full stack up, including the client container
up: build metrics-start cluster-start cluster-wait server-start gen-config client-start
    @just summary

# Re-invoked rather than composed, because CLUSTER_ENABLE_PIX has to be set before `up`'s
# dependencies are evaluated — it steers both the cluster flag and which PIX block gen-config emits.
# Bring the full stack up with PIX enabled end to end (see pix/run.sh)
up-pix:
    CLUSTER_ENABLE_PIX=1 just up

# `env -u`: `set export` would hand the nested just this recipe's own HOPRD_BIN, keeping the
# pix-test binary instead of recomputing the curvy one from CLUSTER_PIX_POOL.
# `up-pix` against the Curvy pool: the Curvy stack comes up first, and the cluster runs on its chain.
# Bring the full stack up with PIX settling through Curvy (see pix/run.sh)
up-curvy:
    env -u HOPRD_BIN CLUSTER_ENABLE_PIX=1 CLUSTER_PIX_POOL=curvy just build metrics-start curvy-stack-up cluster-start cluster-wait server-start gen-config client-start
    @CLUSTER_ENABLE_PIX=1 CLUSTER_PIX_POOL=curvy just summary

# See the caveat on client-start-on-host before using this instead of `up`
# Bring the full stack up with the client running natively on the host instead of in Docker
up-client-on-host: build-cluster build-server build-client-native metrics-start cluster-start-on-host cluster-wait server-start gen-config client-start-on-host
    @just summary-host-client

# Bring up a cluster + exit server reachable from another machine on the LAN, and generate its client config
up-on-network: build-cluster build-server metrics-start cluster-start-on-network cluster-wait server-start gen-config-on-network
    @just summary-on-network

# Print how to control the running client and component versions
summary:
    @scripts/testenv/summary.sh default

# Print how to control the host-native client and component versions
summary-host-client:
    @scripts/testenv/summary.sh host-client

# Print what to copy/run on the other machine, the required firewall ports, and component versions
summary-on-network:
    @scripts/testenv/summary.sh on-network

# Tear the full stack down and purge client state (cluster always restarts with new identities)
down: client-stop clients-stop client2-stop target-stop server-stop cluster-stop curvy-stack-down metrics-stop
    @scripts/testenv/client.sh purge-state

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
    @scripts/testenv/logs.sh

# Tail only cluster node logs
node-logs:
    tail -f "{{DATA_DIR}}/logs/"hoprd_*.log
