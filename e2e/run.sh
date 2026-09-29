#!/usr/bin/env bash
# End-to-end browser tests against a live Gnosis VPN stack: for each destination, connect, let the data path settle, drive the browser harness through the tunnel, then disconnect. Aggregates every per-exit record into summary.csv at the end. Default mode drives the testenv's containerised client (`just up`) and runs the browser in a sidecar sharing that container's network namespace. `--client-mode host` instead talks to a client running natively on this host (rotsee, or `just up-client-on-host`) and runs obscura on the host.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

CLIENT_MODE="${CLIENT_MODE:-container}"
CLIENT_CONTAINER="${CLIENT_CONTAINER:-gnosis_vpn-client}"
E2E_IMAGE="${E2E_IMAGE:-gnosis_vpn-e2e}"
E2E_OUT_DIR="${E2E_OUT_DIR:-/tmp/gnosis_vpn-testenv-e2e}"
SIDECAR_NAME="${SIDECAR_NAME:-gnosis_vpn-e2e-run}"
# The wg peer address the client pings; testenv generates 10.129.0.1 from templates/client.toml.tpl, rotsee uses 10.128.0.1.
PING_TARGETS="${PING_TARGETS:-1.1.1.1,10.129.0.1}"
SETTLE_SECS="${SETTLE_SECS:-15}"
DEST_READY_TIMEOUT="${DEST_READY_TIMEOUT:-180}"
WORKER_KEEPALIVE="${WORKER_KEEPALIVE:-2h}"
WORKER_START_TRIES="${WORKER_START_TRIES:-24}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-150}"
DISCONNECT_TIMEOUT="${DISCONNECT_TIMEOUT:-60}"
COOLDOWN_SECS="${COOLDOWN_SECS:-5}"
QUICK=0

# spawn mode only: the suite starts (and stops) gnosis_vpn-root itself, mirroring how
# gnosis_vpn-system_tests owns its daemon rather than attaching to a pre-running one.
GVPN_ROOT_BIN="${GVPN_ROOT_BIN:-}"
GVPN_WORKER_BIN="${GVPN_WORKER_BIN:-}"
GVPN_CONFIG="${GVPN_CONFIG:-}"
GVPN_IDENTITY_FILE="${GVPN_IDENTITY_FILE:-}"
GVPN_IDENTITY_PASS="${GVPN_IDENTITY_PASS:-}"
GVPN_IDENTITY_SAFE="${GVPN_IDENTITY_SAFE:-}"
GVPN_BLOKLI_URL="${GVPN_BLOKLI_URL:-}"
WORKER_USER="${WORKER_USER:-gnosisvpn-dev}"
SPAWN_HOME="${SPAWN_HOME:-/tmp/gnosis_vpn-e2e-home}"
SPAWN_LOG="${SPAWN_LOG:-/tmp/gnosis_vpn-e2e-daemon.log}"
SPAWN_READY_TRIES="${SPAWN_READY_TRIES:-150}" # x2s; a real network takes longer to reach Running than a localcluster
SPAWNED_DAEMON=0
SUDO_KEEPALIVE_PID=""

# host mode only
GVPN_CTL="${GVPN_CTL:-gnosis_vpn-ctl}"
SOCKET_PATH="${SOCKET_PATH:-}"
OBSCURA="${OBSCURA:-obscura}"
CDP_PORT="${CDP_PORT:-9222}"

usage() {
    cat <<'EOF'
Usage: run.sh [options]

Options:
  -d, --destination ID...   Destinations to test (default: every destination the client reports)
      --client-mode MODE    container (default) | host
      --quick               Short profile: one speedtest pass, 20s down / 10s up, 2 crawl hops
      --out-dir DIR         Parent output directory (default $E2E_OUT_DIR)
  -h, --help                Show this help

Environment: CLIENT_CONTAINER, E2E_IMAGE, E2E_OUT_DIR, PING_TARGETS, SETTLE_SECS,
CONNECT_TIMEOUT, NAV_TIMEOUT_MS, DOWNLOAD_SECS, UPLOAD_SECS, DWELL_MS, CRAWL_START,
CRAWL_HOPS, YT_VIDEO_INDEX. Host mode also reads GVPN_CTL, SOCKET_PATH, OBSCURA.
EOF
}

EXITS=()
while [ $# -gt 0 ]; do
    case "$1" in
    -d | --destination)
        shift
        while [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; do
            EXITS+=("$1")
            shift
        done
        ;;
    --client-mode)
        CLIENT_MODE="$2"
        shift 2
        ;;
    --out-dir)
        E2E_OUT_DIR="$2"
        shift 2
        ;;
    --quick)
        QUICK=1
        shift
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        echo "unknown argument: $1" >&2
        usage >&2
        exit 2
        ;;
    esac
done

case "$CLIENT_MODE" in
container | host | spawn) ;;
*)
    echo "--client-mode must be 'container' or 'host'" >&2
    exit 2
    ;;
esac

if [ "$QUICK" -eq 1 ]; then
    export DOWNLOAD_SECS="${DOWNLOAD_SECS:-20}"
    export UPLOAD_SECS="${UPLOAD_SECS:-10}"
    export CRAWL_HOPS="${CRAWL_HOPS:-2}"
    export SPEEDTEST_RUNS="${SPEEDTEST_RUNS:-1}"
fi

# ─── Client control ────────────────────────────────────────────────────────────

ctl() {
    case "$CLIENT_MODE" in
    container) docker exec "$CLIENT_CONTAINER" gnosis_vpn-ctl "$@" ;;
    host | spawn)
        if [ -n "$SOCKET_PATH" ]; then
            "$GVPN_CTL" -s "$SOCKET_PATH" "$@"
        else
            "$GVPN_CTL" "$@"
        fi
        ;;
    esac
}

ctl_json() { ctl -o json "$@" 2>/dev/null; }

is_connected_to() { # is_connected_to <id>
    [ "$(ctl_json status | jq -r '.Status.connected.destination_id // empty')" = "$1" ]
}

is_disconnected() {
    ctl_json status | jq -e '
        .Status
        | (.connected | not)
          and (.connecting | not)
          and (.reconnecting | not)
          and ((.disconnecting | if type == "array" then length == 0 else not end))
    ' >/dev/null
}

wait_for() { # wait_for <fn> <timeout_s> <label> [<arg>]
    local fn="$1" to="$2" label="$3" arg="${4:-}" i=0
    while [ "$i" -lt "$to" ]; do
        "$fn" ${arg:+"$arg"} && return 0
        sleep 3
        i=$((i + 3))
    done
    echo "!! timeout waiting for $label (${to}s)"
    return 1
}

ensure_disconnected() {
    is_disconnected && return 0
    ctl disconnect >/dev/null 2>&1
    wait_for is_disconnected "$DISCONNECT_TIMEOUT" "disconnect"
}

# ─── Preflight ─────────────────────────────────────────────────────────────────

for cmd in jq curl; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "missing required command: $cmd" >&2
        exit 2
    }
done

if [ "$CLIENT_MODE" = "container" ]; then
    command -v docker >/dev/null 2>&1 || {
        echo "missing required command: docker" >&2
        exit 2
    }
    if [ "$(docker inspect "$CLIENT_CONTAINER" 2>/dev/null | jq -r '.[0].State.Running // "false"')" != "true" ]; then
        echo "container '$CLIENT_CONTAINER' is not running — run 'just up' first" >&2
        exit 2
    fi
    docker image inspect "$E2E_IMAGE" >/dev/null 2>&1 || {
        echo "image '$E2E_IMAGE' not found — run 'just build-e2e' first" >&2
        exit 2
    }
else
    command -v "$GVPN_CTL" >/dev/null 2>&1 || {
        echo "missing $GVPN_CTL (set GVPN_CTL)" >&2
        exit 2
    }
fi

# spawn_daemon -> starts gnosis_vpn-root under sudo and waits for its control socket.
# gnosis_vpn-root needs root for the WireGuard interface and routing table, exactly as
# gnosis_vpn-system_tests does; sudo -v first so a long run does not hit a prompt later.
spawn_daemon() {
    local missing=""
    for v in GVPN_ROOT_BIN GVPN_WORKER_BIN GVPN_CONFIG GVPN_IDENTITY_FILE GVPN_IDENTITY_PASS GVPN_BLOKLI_URL; do
        [ -z "$(eval printf '%s' "\$$v")" ] && missing="$missing $v"
    done
    if [ -n "$missing" ]; then
        echo "--client-mode spawn needs:$missing" >&2
        exit 2
    fi
    id "$WORKER_USER" >/dev/null 2>&1 || {
        echo "worker user '$WORKER_USER' does not exist (set WORKER_USER); gnosis_vpn-root drops privileges to it" >&2
        exit 2
    }

    sudo -v
    # A tour outlives sudo's 5-minute timestamp, so teardown would re-prompt for a password
    # long after the terminal moved on. Refresh it in the background until cleanup stops us.
    (while :; do
        sudo -n -v 2>/dev/null || exit
        sleep 60
    done) &
    SUDO_KEEPALIVE_PID=$!

    # gnosis_vpn-worker runs as WORKER_USER and needs the identity in a directory it owns —
    # a world-readable copy is not enough, it writes alongside the keypair. system-tests does
    # the same thing (identity into ${worker_home}/.config, chowned to the worker user).
    local identity_path="$SPAWN_HOME/.config/gnosisvpn-hopr.id"
    sudo mkdir -p "$SPAWN_HOME/.config"
    sudo cp "$GVPN_IDENTITY_FILE" "$identity_path"
    # The safe/pass sidecars must travel with the identity: without the .safe the client cannot
    # find its existing safe association and onboards a brand new (unfunded) one instead —
    # run_mode sits in PreparingSafe forever. system-tests stages all three for this reason.
    [ -n "$GVPN_IDENTITY_SAFE" ] && sudo cp "$GVPN_IDENTITY_SAFE" "$SPAWN_HOME/.config/gnosisvpn-hopr.safe"
    printf %s "$GVPN_IDENTITY_PASS" | sudo tee "$SPAWN_HOME/.config/gnosisvpn-hopr.pass" >/dev/null
    sudo chown -R "$WORKER_USER" "$SPAWN_HOME"
    sudo chmod 0700 "$SPAWN_HOME/.config"
    sudo chmod 0600 "$identity_path"

    echo "-- spawning gnosis_vpn-root (worker user $WORKER_USER, log $SPAWN_LOG)"
    sudo RUST_LOG="${DAEMON_LOG_LEVEL:-info,gnosis_vpn_root=debug,gnosis_vpn_lib=debug}" \
        GNOSISVPN_HOME="$SPAWN_HOME" \
        "$GVPN_ROOT_BIN" \
        --config-path "$GVPN_CONFIG" \
        --hopr-blokli-url "$GVPN_BLOKLI_URL" \
        --worker-binary "$GVPN_WORKER_BIN" \
        --worker-user "$WORKER_USER" \
        --allow-insecure \
        --client-autostart 4h \
        --log-file "$SPAWN_LOG" \
        --hopr-identity-file "$identity_path" \
        --hopr-identity-pass "$GVPN_IDENTITY_PASS" &
    SPAWNED_DAEMON=1

    # The socket answers while run_mode is still Init, so waiting on it alone starts the tour
    # against a client that has not finished coming up. Wait for Running.
    local i=0
    while [ "$i" -lt "$SPAWN_READY_TRIES" ]; do
        if ctl_json status 2>/dev/null | jq -e '.Status.run_mode | objects | has("Running")' >/dev/null 2>&1; then
            echo "-- daemon up (run_mode Running)"
            return 0
        fi
        sleep 2
        i=$((i + 1))
    done
    echo "daemon did not expose its control socket within $((SPAWN_READY_TRIES * 2))s (see $SPAWN_LOG)" >&2
    exit 2
}

# Container mode gets its node deps baked in by the Dockerfile; host/spawn drive the harness
# straight off this checkout, so they need node_modules present locally.
if [ "$CLIENT_MODE" != "container" ] && [ ! -d "$HERE/node_modules" ]; then
    command -v npm >/dev/null 2>&1 || {
        echo "npm not found — needed to install the harness deps for ${CLIENT_MODE} mode (try: nix develop)" >&2
        exit 2
    }
    echo "-- installing harness node deps (npm ci)"
    (cd "$HERE" && npm ci --omit=dev) || {
        echo "npm ci failed in $HERE" >&2
        exit 2
    }
fi

[ "$CLIENT_MODE" = "spawn" ] && spawn_daemon

if ! ctl_json status | jq -e '.Status' >/dev/null; then
    echo "cannot reach the client's control socket via ${CLIENT_MODE} mode" >&2
    exit 2
fi

# The worker returns to idle mode once its keep-alive expires (GNOSISVPN_CLIENT_AUTOSTART),
# which leaves the socket answering but run_mode "NotRunning" and every route_health null.
if ctl_json status | jq -e '.Status.run_mode == "NotRunning"' >/dev/null; then
    echo "-- worker idle; starting client (keep-alive ${WORKER_KEEPALIVE})"
    ctl start-client "$WORKER_KEEPALIVE" >/dev/null || {
        echo "failed to start the client worker" >&2
        exit 2
    }
    for _ in $(seq 1 "$WORKER_START_TRIES"); do
        ctl_json status | jq -e '.Status.run_mode | objects | has("Running")' >/dev/null && break
        sleep 5
    done
fi

if [ ${#EXITS[@]} -eq 0 ]; then
    # not mapfile: macOS ships bash 3.2
    while IFS= read -r dest_id; do
        [ -n "$dest_id" ] && EXITS+=("$dest_id")
    done < <(ctl_json status | jq -r '.Status.destinations[].destination.id')
fi
if [ ${#EXITS[@]} -eq 0 ]; then
    echo "no destinations configured on the client" >&2
    exit 2
fi

# ─── Run setup ─────────────────────────────────────────────────────────────────

# Drawn once per run so every exit is compared on the same video.
YT_VIDEO_INDEX="${YT_VIDEO_INDEX:-$RANDOM}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUTDIR="$E2E_OUT_DIR/$TS"
mkdir -p "$OUTDIR"
echo "== e2e run $TS -> $OUTDIR =="
echo "== destinations: ${EXITS[*]} =="

STARTED_OBSCURA=""
cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    [ "$CLIENT_MODE" = "container" ] && docker rm -f "$SIDECAR_NAME" >/dev/null 2>&1
    [ -n "$STARTED_OBSCURA" ] && kill "$STARTED_OBSCURA" 2>/dev/null
    ctl disconnect >/dev/null 2>&1
    # We started the daemon, so we stop it — leaving a full-tunnel client running would
    # keep capturing this machine's traffic after the suite exits.
    if [ "$SPAWNED_DAEMON" -eq 1 ]; then
        echo "-- stopping spawned gnosis_vpn-root"
        [ -n "$SUDO_KEEPALIVE_PID" ] && kill "$SUDO_KEEPALIVE_PID" 2>/dev/null
        sudo pkill -f "$(basename "$GVPN_ROOT_BIN")" 2>/dev/null || true
        sudo pkill -f "$(basename "$GVPN_WORKER_BIN")" 2>/dev/null || true
    fi
    exit "$rc"
}
trap cleanup EXIT INT TERM

if [ "$CLIENT_MODE" != "container" ]; then
    if ! curl -sf --max-time 3 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; then
        echo "== starting obscura serve =="
        "$OBSCURA" serve --port "$CDP_PORT" --stealth >"$OUTDIR/obscura-serve.log" 2>&1 &
        STARTED_OBSCURA=$!
        for _ in $(seq 1 20); do
            curl -sf --max-time 2 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1 && break
            sleep 0.5
        done
    fi
fi

# repo_commit <dir> -> short rev, or empty if it isn't a checkout
repo_commit() { git -C "$1" rev-parse --short HEAD 2>/dev/null; }

ensure_disconnected >/dev/null
BASELINE_TRACE="$(curl -s --max-time 8 https://cloudflare.com/cdn-cgi/trace 2>/dev/null | tr '\n' ';')"

jq -n \
    --arg run_ts "$TS" \
    --arg exits "$(
        IFS=,
        echo "${EXITS[*]}"
    )" \
    --arg client_mode "$CLIENT_MODE" \
    --arg testenv_commit "$(repo_commit "$HERE/..")" \
    --arg client_commit "$(repo_commit "${GVPN_CLIENT_DIR:-$HERE/../../gnosis_vpn-client}")" \
    --arg server_commit "$(repo_commit "${GVPN_SERVER_DIR:-$HERE/../../gnosis_vpn-server}")" \
    --arg hoprd_commit "$(repo_commit "${HOPRD_DIR:-$HERE/../../hoprd}")" \
    --arg cluster_size "${CLUSTER_SIZE:-}" \
    --arg server_count "${SERVER_COUNT:-}" \
    --arg hops "${HOPS:-}" \
    --arg quick "$QUICK" \
    --arg ping_targets "$PING_TARGETS" \
    --arg machine "$(uname -sm)" \
    --arg disconnected_baseline_trace "$BASELINE_TRACE" \
    '{
       run_ts: $run_ts, exits: $exits, client_mode: $client_mode, quick: ($quick == "1"),
       commits: { testenv: $testenv_commit, gnosis_vpn_client: $client_commit,
                  gnosis_vpn_server: $server_commit, hoprd: $hoprd_commit },
       stack: { cluster_size: $cluster_size, server_count: $server_count, hops: $hops },
       ping_targets: $ping_targets, machine: $machine,
       disconnected_baseline_trace: $disconnected_baseline_trace,
       notes: "headless YouTube/CNN measure page+player load, not DRM video decode; speedtest is an in-page fetch against speed.cloudflare.com __down/__up"
     }' >"$OUTDIR/meta.json"
echo "== wrote meta.json (baseline egress: $BASELINE_TRACE) =="

# ─── Per-exit loop ─────────────────────────────────────────────────────────────

# drive_harness <exit> -> runs the browser harness, writing <OUTDIR>/<exit>.json
drive_harness() {
    local exit_id="$1"
    if [ "$CLIENT_MODE" = "container" ]; then
        docker rm -f "$SIDECAR_NAME" >/dev/null 2>&1
        docker run --rm --name "$SIDECAR_NAME" \
            --network "container:$CLIENT_CONTAINER" \
            --volume "$OUTDIR:/out" \
            --env EXIT="$exit_id" \
            --env OUTDIR=/out \
            --env PING_TARGETS="$PING_TARGETS" \
            --env YT_VIDEO_INDEX="$YT_VIDEO_INDEX" \
            ${NAV_TIMEOUT_MS:+--env NAV_TIMEOUT_MS="$NAV_TIMEOUT_MS"} \
            ${DOWNLOAD_SECS:+--env DOWNLOAD_SECS="$DOWNLOAD_SECS"} \
            ${UPLOAD_SECS:+--env UPLOAD_SECS="$UPLOAD_SECS"} \
            ${DWELL_MS:+--env DWELL_MS="$DWELL_MS"} \
            ${CRAWL_START:+--env CRAWL_START="$CRAWL_START"} \
            ${CRAWL_HOPS:+--env CRAWL_HOPS="$CRAWL_HOPS"} \
            ${SPEEDTEST_RUNS:+--env SPEEDTEST_RUNS="$SPEEDTEST_RUNS"} \
            "$E2E_IMAGE"
    else
        EXIT="$exit_id" OUTDIR="$OUTDIR" \
            PING_TARGETS="$PING_TARGETS" YT_VIDEO_INDEX="$YT_VIDEO_INDEX" \
            CDP="ws://127.0.0.1:${CDP_PORT}/devtools/browser" \
            node "$HERE/harness.mjs"
    fi
}

# run_harness <exit> -> drives the browser, keeping its output and guaranteeing a record.
# Without this a harness crash left no <exit>.json at all, so the run aggregated to an empty
# summary with nothing explaining why.
run_harness() {
    local exit_id="$1" rc=0
    drive_harness "$exit_id" 2>&1 | tee "$OUTDIR/$exit_id.harness.log"
    rc="${PIPESTATUS[0]}"
    if [ ! -f "$OUTDIR/$exit_id.json" ]; then
        echo "!! harness produced no record for $exit_id (exit $rc); see $exit_id.harness.log"
        fatal_record "$exit_id" "harness failed (exit $rc) — see $exit_id.harness.log"
    fi
}

# fatal_record <exit> <reason> -> the per-exit JSON aggregate.mjs expects when a run never started
fatal_record() {
    jq -n --arg exit "$1" --arg err "$2" '{exit: $exit, fatal_error: $err}' >"$OUTDIR/$1.json"
}

for EXIT_ID in "${EXITS[@]}"; do
    echo ""
    echo "======================================================================"
    echo "== DESTINATION $EXIT_ID  $(date -u +%H:%M:%SZ)"
    echo "======================================================================"

    ensure_disconnected

    # Skip fast instead of burning CONNECT_TIMEOUT: route_health already knows when a
    # destination cannot be dialled (NeedsChannel with too few relays, Unrecoverable/NotAllowed
    # for a hop count the network refuses, NeedsPeering while the client is still warming up).
    # NeedsPeering/NoHealth are warm-up states, not verdicts — poll until the destination is
    # dialable (or clearly is not) rather than skipping the moment the client is still settling.
    rh_waited=0
    while :; do
        rh_state="$(ctl_json status | jq -r --arg id "$EXIT_ID" '
            .Status.destinations[] | select(.destination.id == $id)
            | .route_health.state.state // .route_health.state // "unknown"')"
        case "$rh_state" in
        ReadyToConnect | Connecting | Routable) break ;;
        esac
        [ "$rh_waited" -ge "$DEST_READY_TIMEOUT" ] && break
        [ "$rh_waited" -eq 0 ] && echo "-- waiting up to ${DEST_READY_TIMEOUT}s for $EXIT_ID to become dialable (state: $rh_state)"
        sleep 5
        rh_waited=$((rh_waited + 5))
    done
    rh_reason="$(ctl_json status | jq -r --arg id "$EXIT_ID" '
        .Status.destinations[] | select(.destination.id == $id)
        | .route_health.state.reason // .route_health.last_error // ""')"
    case "$rh_state" in
    ReadyToConnect | Connecting | Routable) ;;
    *)
        echo "!! $EXIT_ID not connectable (state: $rh_state${rh_reason:+, reason: $rh_reason}) — skipping"
        fatal_record "$EXIT_ID" "not connectable (state: $rh_state${rh_reason:+, reason: $rh_reason})"
        continue
        ;;
    esac

    echo "-- connect $EXIT_ID"
    if ! ctl connect "$EXIT_ID"; then
        echo "!! connect rejected"
        fatal_record "$EXIT_ID" "connect rejected"
        ensure_disconnected
        continue
    fi
    if ! wait_for is_connected_to "$CONNECT_TIMEOUT" "connect $EXIT_ID" "$EXIT_ID"; then
        fatal_record "$EXIT_ID" "connect timeout"
        ensure_disconnected
        continue
    fi

    # The tunnel is up before it is usable: SURBs still have to reach the exit, so probing
    # immediately measures a starved path (or fails outright). Wait before any browsing/speedtest.
    echo "-- connected; settling ${SETTLE_SECS}s for SURBs to reach the exit"
    sleep "$SETTLE_SECS"

    # Recorded as metadata only. In testenv every exit NATs through this host, so loc carries no signal and is never used to skip a destination.
    TRACE="$(curl -s --max-time 20 https://cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -E '^(ip|loc|colo)=' | tr '\n' ' ')"
    echo "-- host egress trace: ${TRACE:-none}"

    ctl_json nerd-stats >"$OUTDIR/$EXIT_ID.nerdstats.json" || true

    run_harness "$EXIT_ID"

    echo "-- disconnect $EXIT_ID"
    ctl disconnect >/dev/null 2>&1
    wait_for is_disconnected "$DISCONNECT_TIMEOUT" "disconnect $EXIT_ID"
    sleep "$COOLDOWN_SECS"
done

echo ""
echo "== aggregating =="
EXIT_ORDER="$(
    IFS=,
    echo "${EXITS[*]}"
)"
if [ "$CLIENT_MODE" = "container" ]; then
    # keeps node off the host's dependency list — the image already has it
    docker run --rm \
        --volume "$OUTDIR:/out" \
        --env OUTDIR=/out \
        --env EXIT_ORDER="$EXIT_ORDER" \
        --entrypoint node \
        "$E2E_IMAGE" /app/aggregate.mjs
else
    OUTDIR="$OUTDIR" EXIT_ORDER="$EXIT_ORDER" node "$HERE/aggregate.mjs"
fi

echo "== done -> $OUTDIR =="
