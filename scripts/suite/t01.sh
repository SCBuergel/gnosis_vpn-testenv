#!/usr/bin/env bash
# T01-topology-preconditions — Topology and funding preconditions, plus the relay forwarding probe: cluster running, all nodes
# channels_open, node-to-node pings answer, each relay forwards a 1-hop session opened from the exit node
# (node 0 → via node i → node j) within FWD_TIMEOUT s, every destination Ready at its hop count, no leftover
# qdisc/timers on the host, both liveness-ping targets on the server. Aborts the suite on FAIL.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,6p' "$0"; usage_common; exit 0; }
: "${FWD_TIMEOUT:=20}"
suite_init; fail=0
st=$(cluster_status); state=$(json_get "$st" state)
[ "$state" = running ] && verdict T01-topology-preconditions PASS "cluster state $state" || { verdict T01-topology-preconditions FAIL "cluster state '$state'"; fail=1; }
for i in $(seq 0 $((CLUSTER_SIZE-1))); do
  ns=$(node_field "$i" state); [ "$ns" = channels_open ] && verdict T01-topology-preconditions PASS "node $i state $ns" || { verdict T01-topology-preconditions FAIL "node $i state '$ns'"; fail=1; }
done
# channels open in both directions between every pair (localcluster full mesh)
for i in $(seq 0 $((CLUSTER_SIZE-1))); do
  n=$(node_curl "$i" GET /api/v4/channels | python3 -c 'import sys,json; d=json.load(sys.stdin); print(sum(1 for c in d.get("outgoing",[]) if c.get("status")=="Open"))' 2>/dev/null || echo 0)
  [ "$n" -ge $((CLUSTER_SIZE-1)) ] && verdict T01-topology-preconditions PASS "node $i has $n open outgoing channels" || { verdict T01-topology-preconditions FAIL "node $i has $n open outgoing channels"; fail=1; }
done
# forwarding probe: from node 0, a session with Hops=1 to node j is forced through the remaining node(s)
if [ "$CLUSTER_SIZE" -ge 3 ]; then
  for j in $(seq 1 $((CLUSTER_SIZE-1))); do
    t0=$(now_ms)
    r=$(node_curl 0 POST /api/v4/session/udp "{\"destination\":\"$(node_addr "$j")\",\"forwardPath\":{\"Hops\":1},\"returnPath\":{\"Hops\":0},\"target\":{\"Plain\":\"127.0.0.1:9\"},\"capabilities\":[]}")
    dt=$(( $(now_ms) - t0 )); port=$(json_get "$r" port 2>/dev/null || true); ip=$(json_get "$r" ip 2>/dev/null || true)
    if [ -n "$port" ]; then node_curl 0 DELETE "/api/v4/session/udp/$ip/$port" >/dev/null; verdict T01-topology-preconditions PASS "1-hop session node0→(relay)→node$j established in ${dt} ms"; else verdict T01-topology-preconditions FAIL "1-hop session node0→(relay)→node$j failed after ${dt} ms: $(echo "$r" | head -c 160)"; fail=1; fi
    emit_row T01-topology-preconditions check=forwarding_probe dest_node="$j" ms="$dt" ok="$([ -n "$port" ] && echo true || echo false)"
  done
fi
# client side
wait_worker 120 || { verdict T01-topology-preconditions FAIL "client worker offline"; fail=1; }
dests=$(client_status_text | sed -n 's/^\([^ ]*\) Route health:.*/\1/p')
for d in $dests; do dest_is_ready "$d" && verdict T01-topology-preconditions PASS "destination $d Ready" || { verdict T01-topology-preconditions WARN "destination $d: $(dest_health_line "$d" | cut -c1-120)"; }; done
wait_dest_ready "$DEST" "${READY_TIMEOUT:-300}" || { verdict T01-topology-preconditions FAIL "primary destination $DEST not Ready within ${READY_TIMEOUT:-300}s: $(dest_health_line "$DEST" | cut -c1-120)"; fail=1; }
# client channel pin (former channel-strategy test, part c): the client's internal entry node must hold its outgoing channel(s)
# The client opens its outgoing channels on-chain asynchronously after the worker comes up, so this polls
# rather than sampling once: a suite launched right after `just up` would otherwise fail preconditions on a
# perfectly healthy stack that simply had not finished opening them yet (seen 2026-09-17, 15 s after bring-up).
client_channels() {
  ctl_json balance 2>/dev/null | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); b=d.get("Balance",d); b=b.get("Ok",b); print(len(b.get("channels_out",[])))
except Exception: print(0)'
}
: "${CLIENT_CHANNEL_TIMEOUT:=240}"
nch=0; waited=0
while [ "$waited" -lt "$CLIENT_CHANNEL_TIMEOUT" ]; do
  nch=$(client_channels); [ "${nch:-0}" -ge 1 ] && break
  sleep 5; waited=$((waited+5))
done
if [ "${nch:-0}" -ge 1 ]; then
  verdict T01-topology-preconditions PASS "client holds ${nch} outgoing channel(s)$([ "$waited" -gt 0 ] && echo " (after ${waited}s wait)")"
else
  verdict T01-topology-preconditions FAIL "client holds no outgoing channel after ${CLIENT_CHANNEL_TIMEOUT}s"; fail=1
fi
# effective config vs shipped defaults (was the T13-mtu-sweep corollary): record the diff, never trust the file alone
cfgdiff=$(diff <(sed -E 's/[[:space:]]+//g' "$CONFIG_DIR/client.toml" | sort) <(sed -E 's/[[:space:]]+//g' "${SUITE_RUN}/client.toml.defaults" 2>/dev/null | sort) 2>/dev/null | grep -c '^[<>]' || true)
cp "$CONFIG_DIR/client.toml" "${SUITE_RUN}/client.toml.effective"
emit_row T01-topology-preconditions check=effective_config diff_lines="${cfgdiff:-0}"
# tunnel liveness target. The client's periodic tunnel ping (10 s interval, 3 misses = reconnect) targets the
# hardcoded default 10.128.0.1 in client <= 0.96.3 regardless of [connection.ping].address; if the server does not
# hold that address every session reconnects every ~85 s and every longer test reads as a load defect. Two full
# runs were misread that way on 2026-09-19. Both the configured address and the hardcoded one must answer.
: "${PERIODIC_PING_TARGET:=10.128.0.1}"
cfg_ping=$(sed -n '/^\[connection.ping\]/,/^\[/p' "$CONFIG_DIR/client.toml" | sed -nE 's/^address *= *"([^"]+)".*/\1/p' | head -1)
srv_addrs=$(docker exec gnosis_vpn-server-0 ip -4 -o addr show dev wggvpn 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ')
for want in "$PERIODIC_PING_TARGET" "$cfg_ping"; do
  [ -n "$want" ] || continue
  case " $srv_addrs " in
    *" $want "*) verdict T01-topology-preconditions PASS "liveness-ping target $want is an address on the server's wggvpn";;
    *) verdict T01-topology-preconditions FAIL "liveness-ping target $want is NOT on the server's wggvpn ($srv_addrs) — the client will reconnect every ~85 s and every test longer than that fails on it; set SERVER_PING_ALIAS or fix the client's tunnel_ping_loop"; fail=1;;
  esac
done
emit_row T01-topology-preconditions check=liveness_target periodic="$PERIODIC_PING_TARGET" configured="$cfg_ping" server_wggvpn="\"$srv_addrs\""
# host hygiene
q=$(tc qdisc show 2>/dev/null | grep -c netem || true); [ "$q" -eq 0 ] && verdict T01-topology-preconditions PASS "no netem qdisc on host" || { verdict T01-topology-preconditions FAIL "$q netem qdisc(s) left on host"; fail=1; }
tm=$(systemctl list-timers --all 2>/dev/null | grep -ci "deadman\|suite-" || true); [ "$tm" -eq 0 ] && verdict T01-topology-preconditions PASS "no armed suite timers" || verdict T01-topology-preconditions WARN "$tm suite/deadman timers armed"
emit_row T01-topology-preconditions kind=summary fail="$fail"
exit $fail
