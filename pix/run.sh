#!/usr/bin/env bash
#
# system-test-pix — drive a full PIX cycle through the VPN tunnel and assert the exit was paid.
#
# PIX makes an exit earn per byte it delivers back to the client. The exchange is: the exit asks the
# client to commit to an SSA, the client deposits `price_per_byte × quota` to a stealth address, the
# exit watches that address and defuses its kill switch when the deposit lands, downstream packets
# carry the SSA's shares home on spent SURBs until the exit can reconstruct the stealth key, and the
# exit sweeps the deposit into its Safe. One cycle covers exactly `quota` bytes of exit → client
# data, so the exit's income and the traffic it served are the same number seen from two sides --
# which is what this test asserts.
#
# It drives the containerised client (`just up-pix`), so no sudo and no host routing changes: the
# traffic is ICMP from inside the client container's own network namespace, where the full-tunnel
# routes live. Everything asserted is read from the exit's own Prometheus endpoint and REST API and
# from `gnosis_vpn-ctl` -- nothing is computed here.
#
#   just up-pix
#   just system-test-pix
#   just system-test-pix --seconds 300   # a longer window, so more cycles
#
# Against the Curvy pool (`just up-curvy`) the same cycle runs and the same counters are asserted, but
# the money moves differently: the client's Safe pays once, shielding a float into the Curvy vault
# that every deposit is then allocated out of, and the vault pays the exit less its withdrawal fee.
# So the exit's income is asserted net of that fee, and the client's Safe is reported, not asserted.
# The pool is read off the client image `up-curvy` starts; CLUSTER_PIX_POOL overrides it.
#
# Requires jq, curl, bc and docker. Budget ~5 minutes after the stack is up.

set -uo pipefail

# ─── Configuration ─────────────────────────────────────────────────────────────

LOCALCLUSTER_BIN="${LOCALCLUSTER_BIN:-../hoprd/result-localcluster/bin/hoprd-localcluster}"
DATA_DIR="${DATA_DIR:-/tmp/hopr-nodes}"
CONFIG_DIR="${CONFIG_DIR:-/tmp/gnosis_vpn-testenv}"
CLIENT_CONTAINER="${CLIENT_CONTAINER:-gnosis_vpn-client}"

# The exit's WireGuard interface address, from gnosis_vpn-server/docker/wggvpn.conf. Every echo reply
# from it is one HOPR packet home, and therefore one PIX share.
TUNNEL_GATEWAY="${TUNNEL_GATEWAY:-10.129.0.1}"

# Destination to connect to; empty picks the first one the client reports as ready.
DESTINATION="${DESTINATION:-}"

# Cycles are paced by the SSA exchange, not by bytes: each one waits on a deposit landing on chain
# and on enough of its shares riding home, so a burst of traffic buys no more of them than a trickle.
# What buys cycles is wall-clock time with traffic flowing, hence a duration rather than a count.
PING_SECONDS="${PING_SECONDS:-180}"
PING_INTERVAL="${PING_INTERVAL:-0.2}"
# Reply + ICMP/IP headers + WireGuard overhead has to stay under the 1038 B HOPR payload, or one
# reply costs two packets and the byte accounting below stops being a floor.
PING_SIZE="${PING_SIZE:-900}"

READY_TIMEOUT="${READY_TIMEOUT:-600}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-300}"
DISCONNECT_TIMEOUT="${DISCONNECT_TIMEOUT:-60}"
# Deposit tracking polls for up to 30 s and the sweep is a further transaction, so give the last
# in-flight cycle room to land after the traffic stops. A Curvy sweep is a withdrawal proof plus its
# transaction on top, so that pool gets longer (see below).
SETTLE_TIMEOUT="${SETTLE_TIMEOUT:-}"

# Ceiling on the Curvy vault's withdrawal fee, in basis points, that the exit's income is allowed
# to lose to it. The pinned local chain charges 20; hoprd's own soak sizes against the same 100.
CURVY_FEE_CEILING_BPS="${CURVY_FEE_CEILING_BPS:-100}"

while [ $# -gt 0 ]; do
    case "$1" in
    --seconds) PING_SECONDS="$2"; shift 2 ;;
    --destination) DESTINATION="$2"; shift 2 ;;
    --ping-size) PING_SIZE="$2"; shift 2 ;;
    -h | --help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# ─── Helpers ───────────────────────────────────────────────────────────────────

ctl() { docker exec "$CLIENT_CONTAINER" gnosis_vpn-ctl "$@"; }
ctl_json() { ctl -o json "$@" 2>/dev/null; }

status_json() { "$LOCALCLUSTER_BIN" status --data-dir "$DATA_DIR" 2>/dev/null; }

# Sum a metric across label sets. `_total` is optional: OpenTelemetry's Prometheus exporter appends
# it or not depending on version, so both spellings have to match.
metric() { # metric <scrape file> <name> [<label substring>]
    local file="$1" name="$2" label="${3:-}" lines
    [ -r "$file" ] || { echo 0; return; }
    lines=$(grep -E "^${name}(_total)?[ {]" "$file" 2>/dev/null)
    [ -n "$label" ] && lines=$(printf '%s\n' "$lines" | grep -F -- "$label")
    printf '%s\n' "$lines" | awk '{ s += $NF } END { printf "%.0f", s + 0 }'
}

# A balance field from the exit's REST API, unit stripped ("12.5 wxHOPR" -> "12.5").
exit_balance() { # exit_balance <field>
    curl -s --max-time 5 "${EXIT_API}/api/v4/account/balances" 2>/dev/null |
        jq -r ".$1 // empty" 2>/dev/null | awk 'NF { print $1; exit }'
}

client_safe_wxhopr() {
    ctl_json balance | jq -r '.Balance.Ok.safe // empty' | awk 'NF { print $1; exit }'
}

# bc with a sane default scale, so 21-digit wxHOPR amounts do not overflow shell arithmetic.
calc() { printf 'scale=12; %s\n' "$1" | bc -l; }
ge() { [ "$(calc "$1 >= $2")" = "1" ]; }

is_connected_to() { [ "$(ctl_json status | jq -r '.Status.connected.destination_id // empty')" = "$1" ]; }
is_disconnected() { [ -z "$(ctl_json status | jq -r '.Status.connected.destination_id // empty')" ]; }

wait_for() { # wait_for <fn> <timeout_s> <label> [<arg>]
    local fn="$1" to="$2" label="$3" arg="${4:-}" i=0
    while [ "$i" -lt "$to" ]; do
        "$fn" ${arg:+"$arg"} && return 0
        sleep 3
        i=$((i + 3))
    done
    echo "!! timed out waiting for ${label} (${to}s)" >&2
    return 1
}

FAILURES=0
check() { # check <ok?> <description> <detail>
    if [ "$1" = "1" ]; then
        printf '  PASS  %-46s %s\n' "$2" "$3"
    else
        printf '  FAIL  %-46s %s\n' "$2" "$3"
        FAILURES=$((FAILURES + 1))
    fi
}

cleanup() {
    [ -n "${CONNECTED:-}" ] || return 0
    echo "-- disconnect"
    ctl disconnect > /dev/null 2>&1
    wait_for is_disconnected "$DISCONNECT_TIMEOUT" "disconnect" || true
}
trap cleanup EXIT

# ─── Preflight ─────────────────────────────────────────────────────────────────

for cmd in jq curl bc docker; do
    command -v "$cmd" > /dev/null 2>&1 || { echo "missing required command: $cmd" >&2; exit 2; }
done

[ -x "$LOCALCLUSTER_BIN" ] || { echo "hoprd-localcluster not found at $LOCALCLUSTER_BIN — run 'just build-cluster'" >&2; exit 2; }

if [ "$(status_json | jq -r '.state // "not_running"')" != "running" ]; then
    echo "cluster is not running — run 'just up-pix' first" >&2
    exit 2
fi

if [ "$(docker inspect "$CLIENT_CONTAINER" 2>/dev/null | jq -r '.[0].State.Running // "false"')" != "true" ]; then
    echo "container '$CLIENT_CONTAINER' is not running — run 'just up-pix' first" >&2
    exit 2
fi

# Which deposit pool this stack settles through: `up-curvy` starts the client from its `pix-curvy`
# image, and the node binary it pairs with is chosen by the same switch.
POOL="${CLUSTER_PIX_POOL:-}"
if [ -z "$POOL" ]; then
    case "$(docker inspect "$CLIENT_CONTAINER" 2>/dev/null | jq -r '.[0].Config.Image // empty')" in
    *:pix-curvy) POOL="curvy" ;;
    *) POOL="test" ;;
    esac
fi
case "$POOL" in
test) SETTLE_TIMEOUT="${SETTLE_TIMEOUT:-180}" ;;
curvy) SETTLE_TIMEOUT="${SETTLE_TIMEOUT:-300}" ;;
*) echo "CLUSTER_PIX_POOL must be 'test' or 'curvy', got '$POOL'" >&2; exit 2 ;;
esac

# Both halves of the switch, checked separately so the error says which one is wrong. A cluster
# without the strategy fails the SSA request and closes the tunnel; a client config without PIX
# connects perfectly and earns nothing, which would otherwise read as "PIX is broken".
if ! grep -qE '^\s+- Pix:' "${DATA_DIR}/hoprd_cfg_0.yaml" 2>/dev/null; then
    echo "the cluster was started without --enable-pix, so its exits run no PIX strategy." >&2
    echo "Run 'just down && just up-pix'." >&2
    exit 2
fi
if ! grep -A2 '^\[connection.pix.ping_main\]' "${CONFIG_DIR}/client.toml" 2>/dev/null | grep -q 'enabled = true'; then
    echo "${CONFIG_DIR}/client.toml has PIX disabled for the main session." >&2
    echo "Run 'just down && just up-pix' to regenerate it." >&2
    exit 2
fi

# The per-SSA quota and price the client config asks for; every assertion below is derived from
# these rather than hard-coded, so retuning the template does not silently invalidate the test.
pix_dim() { grep -A4 '^\[connection.pix.dimensions\]' "${CONFIG_DIR}/client.toml" | awk -v k="$1" '$1 == k { print $3; exit }'; }
NUM_SSA_PARTS=$(pix_dim num_ssa_parts)
SSA_PART_SIZE=$(pix_dim ssa_part_size)
ADDITIONAL_SHARES=$(pix_dim additional_shares)
PRICE_PER_BYTE=$(grep -A3 '^\[pix_strategy\]' "${CONFIG_DIR}/client.toml" | awk '$1 == "price_per_byte" { gsub(/"/, "", $3); print $3; exit }')

# PACKET_PAYLOAD_SIZE — hopr-lib's HoprPacket::PAYLOAD_SIZE.
PAYLOAD_SIZE=1038
QUOTA=$(( NUM_SSA_PARTS * (SSA_PART_SIZE + ADDITIONAL_SHARES) * PAYLOAD_SIZE ))
PER_CYCLE=$(calc "$PRICE_PER_BYTE * $QUOTA")

# ─── Resolve the exit ──────────────────────────────────────────────────────────

echo "-- waiting for destinations"
ready_destination() {
    local id
    # `route_health.state` is internally tagged, so the variant name is a `state` field rather than
    # the object's only key.
    id=$(ctl_json status | jq -r '
        .Status.destinations[]
        | select(.route_health.state.state == "ReadyToConnect")
        | .destination.id' | head -1)
    [ -n "$id" ] && { READY_ID="$id"; return 0; }
    return 1
}
wait_for ready_destination "$READY_TIMEOUT" "a ready destination" || exit 1
DEST="${DESTINATION:-$READY_ID}"

# The destination id is `node-N` for a localcluster exit, and N indexes the status JSON.
EXIT_IDX="${DEST#node-}"
EXIT_API=$(status_json | jq -r --argjson i "$EXIT_IDX" '.nodes[] | select(.id == $i) | .api_url')
[ -n "$EXIT_API" ] && [ "$EXIT_API" != "null" ] || { echo "could not resolve an API url for '$DEST'" >&2; exit 2; }

SCRAPE_BEFORE=$(mktemp); SCRAPE_AFTER=$(mktemp)
trap 'rm -f "$SCRAPE_BEFORE" "$SCRAPE_AFTER"; cleanup' EXIT
curl -s --max-time 5 "${EXIT_API}/metrics" > "$SCRAPE_BEFORE" 2>/dev/null
[ -s "$SCRAPE_BEFORE" ] || { echo "could not scrape ${EXIT_API}/metrics" >&2; exit 2; }

echo "== PIX system test =================================================="
printf '  exit:            %s (%s)\n' "$DEST" "$EXIT_API"
printf '  dimensions:      %s x (%s + %s) -> quota %s B per SSA\n' \
    "$NUM_SSA_PARTS" "$SSA_PART_SIZE" "$ADDITIONAL_SHARES" "$QUOTA"
printf '  pool:            %s\n' "$POOL"
printf '  price:           %s wxHOPR/byte -> %s wxHOPR per cycle\n' "$PRICE_PER_BYTE" "$PER_CYCLE"
printf '  traffic:         %s B ICMP every %ss for %ss through the tunnel to %s\n' \
    "$PING_SIZE" "$PING_INTERVAL" "$PING_SECONDS" "$TUNNEL_GATEWAY"
echo "====================================================================="

# ─── Connect ───────────────────────────────────────────────────────────────────

# The Curvy pool's one Safe payment — the shield — happens on the first deposit, which can land
# before the post-connect baseline below; the client's Safe is baselined here for that pool.
CLIENT_SAFE_BEFORE_CONNECT=$(client_safe_wxhopr)

echo "-- connect ${DEST}"
ctl connect "$DEST" > /dev/null || { echo "connect rejected" >&2; exit 1; }
wait_for is_connected_to "$CONNECT_TIMEOUT" "connect ${DEST}" "$DEST" || exit 1
CONNECTED=1

# Baseline *after* connecting: the cluster stakes its channels out of the same Safe during bootstrap,
# and a baseline taken before that settles reports the staking as negative PIX income.
curl -s --max-time 5 "${EXIT_API}/metrics" > "$SCRAPE_BEFORE" 2>/dev/null
GENERATED_BEFORE=$(metric "$SCRAPE_BEFORE" hopr_strategy_pix_deposit_data 'outcome="generated"')
SWEEPS_BEFORE=$(metric "$SCRAPE_BEFORE" hopr_strategy_pix_sweeps)
KEYS_BEFORE=$(metric "$SCRAPE_BEFORE" hopr_strategy_pix_keys_recovered)
CONFIRMED_BEFORE=$(metric "$SCRAPE_BEFORE" hopr_strategy_pix_deposit_tracking 'outcome="confirmed"')
EXIT_SAFE_BEFORE=$(exit_balance safeHopr)
CLIENT_SAFE_BEFORE=$(client_safe_wxhopr)
printf '   exit safe %s wxHOPR, sweeps %s | client safe %s wxHOPR\n' \
    "$EXIT_SAFE_BEFORE" "$SWEEPS_BEFORE" "$CLIENT_SAFE_BEFORE"

# ─── Traffic ───────────────────────────────────────────────────────────────────

echo "-- driving traffic through the tunnel for ${PING_SECONDS}s"
PING_OUT=$(docker exec "$CLIENT_CONTAINER" \
    ping -w "$PING_SECONDS" -s "$PING_SIZE" -i "$PING_INTERVAL" -W 2 "$TUNNEL_GATEWAY" 2>&1 | tail -3)
RECEIVED=$(printf '%s\n' "$PING_OUT" | sed -n 's/.*[^0-9]\([0-9][0-9]*\) packets received.*/\1/p' | head -1)
[ -n "$RECEIVED" ] || RECEIVED=$(printf '%s\n' "$PING_OUT" | sed -n 's/.*[^0-9]\([0-9][0-9]*\) received.*/\1/p' | head -1)
RECEIVED="${RECEIVED:-0}"
# ICMP payload + 8 B ICMP header + 20 B IPv4 header is what actually crossed the tunnel.
DELIVERED=$(( RECEIVED * (PING_SIZE + 28) ))
printf '   %s replies, %s B delivered downstream\n' "$RECEIVED" "$DELIVERED"

# ─── Settle ────────────────────────────────────────────────────────────────────

echo "-- waiting for in-flight cycles to settle"
last=-1; stable=0; waited=0
while [ "$waited" -lt "$SETTLE_TIMEOUT" ]; do
    curl -s --max-time 5 "${EXIT_API}/metrics" > "$SCRAPE_AFTER" 2>/dev/null
    now=$(metric "$SCRAPE_AFTER" hopr_strategy_pix_sweeps)
    if [ "$now" = "$last" ]; then
        stable=$((stable + 1))
        [ "$stable" -ge 3 ] && [ "$now" -gt "$SWEEPS_BEFORE" ] && break
    else
        stable=0
    fi
    last="$now"
    sleep 5
    waited=$((waited + 5))
done

SWEEPS_AFTER=$(metric "$SCRAPE_AFTER" hopr_strategy_pix_sweeps)
KEYS_AFTER=$(metric "$SCRAPE_AFTER" hopr_strategy_pix_keys_recovered)
CONFIRMED_AFTER=$(metric "$SCRAPE_AFTER" hopr_strategy_pix_deposit_tracking 'outcome="confirmed"')
GENERATED_AFTER=$(metric "$SCRAPE_AFTER" hopr_strategy_pix_deposit_data 'outcome="generated"')
EXIT_SAFE_AFTER=$(exit_balance safeHopr)
CLIENT_SAFE_AFTER=$(client_safe_wxhopr)

N=$(( SWEEPS_AFTER - SWEEPS_BEFORE ))
KEYS=$(( KEYS_AFTER - KEYS_BEFORE ))
CONFIRMED=$(( CONFIRMED_AFTER - CONFIRMED_BEFORE ))
GENERATED=$(( GENERATED_AFTER - GENERATED_BEFORE ))
EXIT_GAIN=$(calc "$EXIT_SAFE_AFTER - $EXIT_SAFE_BEFORE")
CLIENT_SPENT=$(calc "$CLIENT_SAFE_BEFORE - $CLIENT_SAFE_AFTER")
EXPECTED=$(calc "$N * $PER_CYCLE")
if [ "$POOL" = "curvy" ]; then
    # What the exit is owed after the vault's withdrawal fee, at the ceiling.
    EXPECTED_EXIT=$(calc "$EXPECTED * (10000 - $CURVY_FEE_CEILING_BPS) / 10000")
    CLIENT_SPENT=$(calc "$CLIENT_SAFE_BEFORE_CONNECT - $CLIENT_SAFE_AFTER")
else
    EXPECTED_EXIT="$EXPECTED"
fi

# ─── Assertions ────────────────────────────────────────────────────────────────

echo
echo "== results =========================================================="
printf '  cycles swept:    %s\n' "$N"
printf '  exit safe:       %s -> %s  (+%s wxHOPR)\n' "$EXIT_SAFE_BEFORE" "$EXIT_SAFE_AFTER" "$EXIT_GAIN"
if [ "$POOL" = "curvy" ]; then
    printf '  client safe:     %s -> %s  (-%s wxHOPR from before connecting; only a shield moves it)\n' \
        "$CLIENT_SAFE_BEFORE_CONNECT" "$CLIENT_SAFE_AFTER" "$CLIENT_SPENT"
else
    printf '  client safe:     %s -> %s  (-%s wxHOPR)\n' "$CLIENT_SAFE_BEFORE" "$CLIENT_SAFE_AFTER" "$CLIENT_SPENT"
fi
printf '  delivered:       %s B downstream, %s B covered by %s cycle(s)\n' "$DELIVERED" "$(( N * QUOTA ))" "$N"
# Reported rather than asserted: with auto-redeeming on, ticket income can land in the same Safe, so
# a ratio above the cycle count is legitimate. A whole number here is PIX and nothing else.
if [ "$N" -gt 0 ] && [ "$POOL" = "curvy" ]; then
    # A Curvy sweep lands net of the vault's withdrawal fee, so the ratio is not whole; show what one
    # sweep actually credited instead, and the fee that implies.
    LAST_SWEEP=$(grep -E '^hopr_strategy_pix_last_sweep_hopr ' "$SCRAPE_AFTER" | awk '{ print $NF; exit }')
    [ -n "$LAST_SWEEP" ] && printf '  exit gain / sweep: %s wxHOPR (%s per cycle less the vault fee: %s bps)\n' \
        "$LAST_SWEEP" "$PER_CYCLE" "$(printf '%.1f' "$(calc "($PER_CYCLE - $LAST_SWEEP) / $PER_CYCLE * 10000")")"
elif [ "$N" -gt 0 ]; then
    printf '  exit gain / cycle: %s (a whole number means PIX alone moved the Safe)\n' \
        "$(calc "$EXIT_GAIN / $PER_CYCLE")"
fi
echo "---------------------------------------------------------------------"

check "$([ "$N" -ge 1 ] && echo 1 || echo 0)" \
    "exit swept at least one deposit" "sweeps +${N}"
check "$([ "$KEYS" -eq "$N" ] && echo 1 || echo 0)" \
    "one stealth key recovered per sweep" "keys +${KEYS}, sweeps +${N}"
check "$([ "$CONFIRMED" -ge "$N" ] && echo 1 || echo 0)" \
    "every swept deposit was confirmed first" "confirmed +${CONFIRMED} >= sweeps +${N}"
check "$([ "$GENERATED" -ge "$N" ] && echo 1 || echo 0)" \
    "a deposit address per swept cycle" "generated +${GENERATED} >= sweeps +${N}"
# `>=` rather than `==`: --enable-pix leaves auto-redeeming on, so winning tickets also credit the
# exit's Safe. Only hoprd's own session_pix.rs, which disables it, can assert a whole multiple.
# Under Curvy the vault keeps its withdrawal fee, so the floor is the income net of the fee ceiling.
check "$(ge "$EXIT_GAIN" "$EXPECTED_EXIT" && echo 1 || echo 0)" \
    "exit's Safe grew by at least the PIX income" "+${EXIT_GAIN} >= ${EXPECTED_EXIT} wxHOPR"
# Under Curvy the client's Safe pays only when it shields a float into the vault — once, on its first
# deposit, not per cycle — and every later deposit is a private note allocated out of that float.
# Nothing on chain ties the client's Safe to the exit's income; that is the pool's point. So a Safe
# delta says whether this run happened to shield, not whether the client paid: reported, not
# asserted. The payment itself is covered above — every sweep needs a deposit the exit confirmed,
# and only the client's float can have produced it.
if [ "$POOL" = "curvy" ]; then
    printf '  INFO  %-46s %s\n' "client paid from its shielded float" \
        "Safe -${CLIENT_SPENT} wxHOPR this run (the shield, if this run made it)"
else
    check "$(ge "$CLIENT_SPENT" "$EXPECTED" && echo 1 || echo 0)" \
        "client paid what the exit earned" "-${CLIENT_SPENT} >= ${EXPECTED} wxHOPR"
fi
check "$([ "$DELIVERED" -ge "$(( N * QUOTA ))" ] && echo 1 || echo 0)" \
    "income corresponds to data delivered" "${DELIVERED} B >= ${N} x ${QUOTA} B"
echo "====================================================================="

if [ "$FAILURES" -gt 0 ]; then
    echo "${FAILURES} check(s) failed" >&2
    exit 1
fi
echo "all checks passed"
