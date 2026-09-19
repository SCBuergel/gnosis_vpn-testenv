#!/usr/bin/env bash
# T04-fixed-throughput — Fixed-payload throughput matrix (core gate). One session: REPS x (download, upload) against the
# in-cluster target, per-second stall detection, client-log error counters, telemetry deltas.
# Scoring: the error counters are absolute gates (a healthy stack has zero reassembly failures and zero
# reconnects); the throughput is scored as a delta against the run's reference cell inside T03-repeatability-baseline's band,
# because on a single host an absolute Mbit/s threshold is a host artifact.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,7p' "$0"; usage_common; exit 0; }
: "${WAIT_AFTER_CONNECT:=0}" "${LABEL:=t04}" "${TEST:=T04-fixed-throughput}"
suite_init; require_target
undec0=$(telemetry_metric 'hopr_packet_rejected_count{reason="undecodable"}' || true)
connect "$DEST" "$WAIT_AFTER_CONNECT" || { verdict "$TEST" FAIL "connect failed"; exit 1; }
node_sampler_start "${LABEL}" 1
summary=$(transfer_series "$LABEL" "$TARGET_IP" "$REPS" "$BYTES" "$CAP" "$TEST")
node_sampler_stop
errs=$(log_errors "$LOG_SINCE"); save_client_log "${LABEL}" "$LOG_SINCE"
disconnect
undec1=$(telemetry_metric 'hopr_packet_rejected_count{reason="undecodable"}' || true)
emit_row "$TEST" label="$LABEL" kind=summary "summary=$summary" "errors=$errs" connect_ms="$CONNECT_MS" wait_after_connect="$WAIT_AFTER_CONNECT" undecodable_before="${undec0:-null}" undecodable_after="${undec1:-null}"
dc=$(json_get "$summary" down_complete); uc=$(json_get "$summary" up_complete)
reas=$(json_get "$errs" reassembly_failed); rec=$(json_get "$errs" reconnects); dec=$(json_get "$errs" decap_error)
# absolute assertions: completion and the discrete error classes
if [ "$dc" -eq "$REPS" ] && [ "$uc" -eq "$REPS" ] && [ "$reas" -eq 0 ] && [ "$rec" -eq 0 ]; then
  verdict "$TEST" PASS "all $REPS transfers complete both ways; reassembly=$reas reconnects=$rec (tunnel-ping timeouts $(json_get "$errs" ping_timeouts)) discards=$(json_get "$errs" frame_discarded) decap=$dec"
else
  verdict "$TEST" FAIL "complete $dc/$REPS down $uc/$REPS up; reassembly=$reas reconnects=$rec (tunnel-ping timeouts $(json_get "$errs" ping_timeouts)) discards=$(json_get "$errs" frame_discarded) decap=$dec"
fi
# relative assertions: throughput against the reference cell, inside T03-repeatability-baseline's band
score_delta "$TEST" down_mbit "$(json_get "$summary" down_median)" higher_better
score_delta "$TEST" up_mbit   "$(json_get "$summary" up_median)"   higher_better
exit $SUITE_FAILED
