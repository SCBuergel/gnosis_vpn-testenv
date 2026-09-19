#!/usr/bin/env bash
# T20-fault-injection — Relay fault injection: loss ladder (tc netem loss on the host toward one relay's P2P/relay port), then a
# full pause (SIGSTOP) of that relay and restore, all during a live call. Records loss per arm, whether the session
# survives and time to recover. Needs root for tc. Env: LOSSES="1 5 20" STEP_S (60; fast 30). Pass: session survives.
source "$(dirname "$0")/lib.sh"
suite_kind diagnostic
[ "${1:-}" = "--help" ] && { sed -n '2,5p' "$0"; usage_common; exit 0; }
: "${LOSSES:=1 5 20}" "${STEP_S:=$(q 60 30)}" "${RELAY:=1}"
suite_init; require_target
[ "$(id -u)" = 0 ] || { verdict T20-fault-injection SKIP "needs root for tc"; exit 0; }
p2p=$(node_field "$RELAY" p2p); port=${p2p##*:}; iface=lo
cleanup() { tc qdisc del dev $iface root 2>/dev/null || true; kill -CONT "$(node_pid "$RELAY")" 2>/dev/null || true; disarm_deadman; }
trap cleanup EXIT
tc qdisc add dev $iface root handle 1: prio 2>/dev/null || true
tc qdisc add dev $iface parent 1:3 handle 30: netem loss 0% 2>/dev/null || true
tc filter add dev $iface protocol ip parent 1:0 prio 1 u32 match ip dport "$port" 0xffff flowid 1:3 2>/dev/null || true
connect "$DEST" 15 || { verdict T20-fault-injection FAIL "connect failed"; exit 1; }
total=$(( (${#LOSSES}+1) * STEP_S + 3*STEP_S ))
in_client_bg "python3 /suite/probes/relprobe.py --host $TARGET_IP --port 8901 --rate-mbit 1.5 --duration $total --size 1200 --iface $WG_IFACE --out ${SUITE_RUN_IN_CLIENT}/t20-call"
sleep "$STEP_S"
for l in $LOSSES; do tc qdisc change dev $iface parent 1:3 handle 30: netem loss "${l}%"; emit_row T20-fault-injection arm="loss$l" t="$(date +%s)"; sleep "$STEP_S"; done
tc qdisc change dev $iface parent 1:3 handle 30: netem loss 0%
kill -STOP "$(node_pid "$RELAY")"; emit_row T20-fault-injection arm=pause t="$(date +%s)"; sleep "$STEP_S"; kill -CONT "$(node_pid "$RELAY")"; emit_row T20-fault-injection arm=restore t="$(date +%s)"; sleep "$((STEP_S*2))"
e=$(log_errors "$LOG_SINCE"); save_client_log t20 "$LOG_SINCE"; still=$(client_is_connected && echo yes || echo no); disconnect
j=$(cat "$SUITE_RUN/t20-call.json" 2>/dev/null || echo '{}')
emit_row T20-fault-injection kind=summary "call=$j" "errors=$e" session_survived="$still" relay="$RELAY" port="$port"
[ "$still" = yes ] && verdict T20-fault-injection PASS "session survived loss ladder [$LOSSES]% and a ${STEP_S}s relay pause; call loss $(json_get "$j" loss_pct)%, stalls>5s $(json_get "$j" stalls_gt_5s), reconnects $(json_get "$e" reconnects)" || verdict T20-fault-injection FAIL "session died during fault injection (reconnects $(json_get "$e" reconnects))"
exit $SUITE_FAILED
