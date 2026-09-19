#!/usr/bin/env bash
# T07-cold-start — cold vs warm session start: one arm measured immediately after connect, one after
# WARM seconds. Also asserts the exit-side session SURB target reached the client's main target before load
# (hopr_session_surb_target_buffer on the exit node).
# WARM is 25 s: past client 0.96.2's 20 s SURB ramp, so the warm arm measures steady state. It was briefly 75 s
# and that was harmful: a long post-connect idle lets the return-path SURBs expire, so the first transfer after
# it dies with a tunnel-ping reconnect (8/8 on two hoprd versions). Never idle longer than the ramp needs.
# The cold arm sets RAMP_WAIT_OPT_OUT=1 — measuring the ramp is the whole point of that arm.
# The real cold-start signal is a COLLAPSE (no completion, decap errors, a reconnect loop), not that the cold
# arm is slower than the warm one: a fresh 0.96.x session measures the SURB ramp, whose first transfer is
# ~0.5x steady state by design (see T15-warmup-knee). Three clean cold starts measured 0.48, 0.34 and 0.25, so the
# ratio is RECORDED with COLD_WARM_FIRST_RATIO as a reference value and no longer gates: every threshold tried
# failed a healthy cold start.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,4p' "$0"; usage_common; exit 0; }
: "${WARM:=$(q 25 25)}" "${COLD_WARM_FIRST_RATIO:=0.35}" "${COLD_WARM_MEDIAN_MULT:=2}" "${COLD_WARM_MEDIAN_FLOOR:=5}"
suite_init; require_target
run_arm() { # arm wait
  local arm=$1 w=$2
  # the cold arm must not inherit the global ramp wait: its job is to measure what a fresh session does
  local opt=0; [ "$w" = 0 ] && opt=1
  RAMP_WAIT_OPT_OUT=$opt connect "$DEST" "$w" || { verdict T07-cold-start FAIL "$arm: connect failed"; return; }
  local tgt; tgt=$(node_metrics 0 | awk '/^hopr_session_surb_target_buffer\{/ {v=$2} END {print v+0}')
  local s; s=$(transfer_series "t07-$arm" "$TARGET_IP" "$(q "$REPS" 1)" "$BYTES" "$CAP" T07-cold-start)
  local e; e=$(log_errors "$LOG_SINCE"); save_client_log "t07-$arm" "$LOG_SINCE"; disconnect
  emit_row T07-cold-start arm="$arm" wait="$w" "summary=$s" "errors=$e" exit_surb_target_before_load="$tgt"
  echo "$s|$e|$tgt"
}
cold=$(run_arm cold 0); warm=$(run_arm warm "$WARM")
cs=${cold%%|*}; ce=$(echo "$cold" | cut -d'|' -f2); ct=${cold##*|}
ws=${warm%%|*}; we=$(echo "$warm" | cut -d'|' -f2)
cd=$(json_get "$cs" down_median); wd=$(json_get "$ws" down_median); cdec=$(json_get "$ce" decap_error); wdec=$(json_get "$we" decap_error)
ccomp=$(json_get "$cs" down_complete); wcomp=$(json_get "$ws" down_complete); crec=$(json_get "$ce" reconnects); wrec=$(json_get "$we" reconnects)
# first transfer of each arm is the cold-start signal; medians hide it behind the recovered reps
cfirst=$(grep -a '"label": "t07-cold", "dir": "down", "rep": 1' "$SUITE_RUN/rows.jsonl" | tail -1 | python3 -c 'import sys,json; print(json.load(sys.stdin)["res"]["mbit"])' 2>/dev/null || echo 0)
wfirst=$(grep -a '"label": "t07-warm", "dir": "down", "rep": 1' "$SUITE_RUN/rows.jsonl" | tail -1 | python3 -c 'import sys,json; print(json.load(sys.stdin)["res"]["mbit"])' 2>/dev/null || echo 0)
msg="cold: first download $cfirst Mbit/s, median $cd, complete $ccomp, decap $cdec, reconnects $crec (tunnel-ping timeouts $(json_get "$ce" ping_timeouts)); warm: first $wfirst, median $wd, complete $wcomp, decap $wdec, reconnects $wrec (tunnel-ping timeouts $(json_get "$we" ping_timeouts)); exit SURB target at cold load start: $ct"
record T07-cold-start "cold/warm first-transfer ratio $(python3 -c "print(round($cfirst/max($wfirst,0.001),2))") (the SURB ramp; 0.48, 0.34 and 0.25 measured on clean cold starts, so it is recorded, not gated; COLD_WARM_FIRST_RATIO=$COLD_WARM_FIRST_RATIO is the value below which it is worth a look)"
python3 -c 'import sys; cf,wf,cc,wc,cd,wd,cr,wr,ratio,mult,floor=map(float,sys.argv[1:12]); ok = cr==0 and wr==0 and cc>=wc and cd<=max(floor,mult*wd); sys.exit(0 if ok else 1)' "$cfirst" "$wfirst" "$ccomp" "$wcomp" "$cdec" "$wdec" "$crec" "$wrec" "$COLD_WARM_FIRST_RATIO" "$COLD_WARM_MEDIAN_MULT" "$COLD_WARM_MEDIAN_FLOOR" \
  && verdict T07-cold-start PASS "$msg" || verdict T07-cold-start FAIL "$msg"
exit $SUITE_FAILED
