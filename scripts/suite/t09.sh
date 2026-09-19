#!/usr/bin/env bash
# T09-impairment-ladder — Return-path impairment (absorbs the former synthetic latency ladder). The exit's return relays are
# impaired live with tc netem on the host: no cluster restart, no chain rebuild.
#   ladder   rungs of delay applied to EVERY relay (equal) vs to ONE relay (gap) — the mixed-RTT effect
#   far      one relay pushed to FAR ms while the other stays near: the extreme of the gap arm
# Gate: every equal-latency cell completes with zero reassembly failures and zero reconnects — the stack must
# survive symmetric latency. Gap and far cells are recorded: the degradation they show is the finding itself.
#
# Relay-set membership is varied by impairment, NOT by opening and closing the exit's channels. A channel close
# is two-phase with a grace period, so a close/reopen inside one run leaves the channel PendingToClose and the
# exit with no usable return relay, which wedges every later test. That was observed and is why this test never
# touches the channel API.
# Env: RUNGS="0 12.5 25 50" (ms one-way) FAR=100 STREAM_S. Needs root for tc.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,12p' "$0"; usage_common; exit 0; }
: "${RUNGS:=$(q "0 25 50" "0 25")}" "${FAR:=100}" "${STREAM_S:=$(q 120 90)}" "${DO_SETS:=1}"
suite_init; require_target
[ "$CLUSTER_SIZE" -ge 3 ] || { verdict T09-impairment-ladder SKIP "needs CLUSTER_SIZE>=3 (exit + two relays)"; exit 0; }
[ "$(id -u)" = 0 ] || { verdict T09-impairment-ladder SKIP "needs root for tc netem"; exit 0; }
relays=$(seq 1 $((CLUSTER_SIZE-1)))
trap 'netem_clear; disarm_deadman' EXIT
# cell LABEL EQUAL(0|1) — measure one impairment cell; equal cells are gated, unequal ones recorded
cell() {
  local label=$1 equal=$2
  connect "$DEST" 15 || { [ "$equal" = 1 ] && verdict T09-impairment-ladder FAIL "$label: connect failed" || record T09-impairment-ladder "$label: connect failed"; return; }
  local s j e
  s=$(transfer_series "t09-$label" "$TARGET_IP" "$(q 3 2)" "$BYTES" "$CAP" T09-impairment-ladder)
  in_client "python3 /suite/probes/streamprobe.py --mode dl --host $TARGET_IP --port 8902 --rate-mbit 3 --duration $STREAM_S --size 1200 --iface $WG_IFACE --out ${SUITE_RUN_IN_CLIENT}/t09-$label.json" >/dev/null 2>&1 || true
  j=$(cat "$SUITE_RUN/t09-$label.json" 2>/dev/null || echo '{}'); e=$(log_errors "$LOG_SINCE"); disconnect
  emit_row T09-impairment-ladder cell="$label" equal="$equal" "summary=$s" "stream=$j" "errors=$e"
  local msg="$label: down $(json_get "$s" down_median) up $(json_get "$s" up_median) Mbit/s, complete $(json_get "$s" down_complete)/$(json_get "$s" up_complete), stream loss $(json_get "$j" loss_pct)%, discards $(json_get "$e" frame_discarded), reassembly $(json_get "$e" reassembly_failed), reconnects $(json_get "$e" reconnects) (tunnel-ping timeouts $(json_get "$e" ping_timeouts))"
  if [ "$equal" = 1 ]; then
    [ "$(json_get "$e" reassembly_failed)" = 0 ] && [ "$(json_get "$e" reconnects)" = 0 ] \
      && verdict T09-impairment-ladder PASS "$msg" || verdict T09-impairment-ladder FAIL "$msg"
  else
    record T09-impairment-ladder "$msg"
  fi
}
# --- ladder: equal delay on all relays vs a gap on one ---
for ms in $RUNGS; do
  spec=""; for r in $relays; do [ "$ms" != 0 ] && spec="$spec $r=$ms"; done
  netem_apply "$spec"; SUITE_LATENCY_MAP="$spec" cell "equal-${ms}ms" 1
  if [ "$ms" != 0 ]; then
    netem_apply "1=$ms"; SUITE_LATENCY_MAP="1=$ms" cell "gap-${ms}ms" 0
  fi
done
# --- far: one relay at FAR ms, the other near (the extreme gap) ---
netem_apply "2=$FAR"; SUITE_LATENCY_MAP="2=$FAR" cell "far-${FAR}ms" 0
netem_clear
exit $SUITE_FAILED
