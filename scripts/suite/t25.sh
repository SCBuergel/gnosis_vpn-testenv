#!/usr/bin/env bash
# T25-knob-ab — Component runtime-knob A/B: restart the cluster with one env var changed (KNOB="K=V"), everything else
# untouched, T04-fixed-throughput both ways. Env: KNOB (default HOPR_INTERNAL_IN_PACKET_PIPELINE_CONCURRENCY=<CPUs×8>), CLIENT_KNOB
# (K=V applied to the client instead, e.g. GNOSISVPN_SURB_RAMP_SECS=0). Informational: prints both cells' medians.
source "$(dirname "$0")/lib.sh"
suite_kind runbook
[ "${1:-}" = "--help" ] && { sed -n '2,5p' "$0"; usage_common; exit 0; }
: "${KNOB:=HOPR_INTERNAL_IN_PACKET_PIPELINE_CONCURRENCY=$(( $(nproc) * 8 ))}" "${CLIENT_KNOB:=}"
suite_init
cell() { # label
  require_target; connect "$DEST" 15 || { record T25-knob-ab "$1: connect failed"; echo '{}'; return; }
  local s e; s=$(transfer_series "t25-$1" "$TARGET_IP" "$(q "$REPS" 2)" "$BYTES" "$CAP" T25-knob-ab); e=$(log_errors "$LOG_SINCE"); disconnect
  emit_row T25-knob-ab arm="$1" knob="$KNOB" client_knob="$CLIENT_KNOB" "summary=$s" "errors=$e"; echo "$s"
}
a=$(cell baseline)
if [ -n "$CLIENT_KNOB" ]; then ( cd "$TESTENV_DIR" && just client-stop >/dev/null 2>&1; CLIENT_EXTRA_ENV="$CLIENT_KNOB" just client-start >/dev/null ); wait_worker 180
else ( cd "$TESTENV_DIR" && CLUSTER_ENV="$KNOB" just cluster-restart >/dev/null 2>&1 ) || { record T25-knob-ab "cluster restart with $KNOB failed"; exit 1; }; wait_worker 180; fi
b=$(cell knob)
( cd "$TESTENV_DIR" && if [ -n "$CLIENT_KNOB" ]; then just client-stop >/dev/null 2>&1; just client-start >/dev/null; else just cluster-restart >/dev/null 2>&1; fi ) || true; wait_worker 180 || true
record T25-knob-ab "baseline down $(json_get "$a" down_median) up $(json_get "$a" up_median); with ${CLIENT_KNOB:-$KNOB}: down $(json_get "$b" down_median) up $(json_get "$b" up_median) Mbit/s"
exit 0
