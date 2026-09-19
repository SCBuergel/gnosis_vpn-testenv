#!/usr/bin/env bash
# T05-loaded-latency — Loaded latency (bufferbloat) and parallel-flow scaling (gate): in-tunnel ping idle, during a saturating
# download, during a saturating upload; then N ∈ {1,3,6} parallel downloads/uploads. Env: PHASE_S (default 30).
# Scoring: an absolute loaded-RTT threshold is meaningless on a shared host, so the p95s are scored as a DELTA
# against the previous run of this same stack, inside T03-repeatability-baseline's band. Two hard assertions:
#   (a) loaded RTT p95 must not regress beyond the band, in either direction;
#   (b) parallel aggregate throughput must not fall as N rises.
# This test must run EARLY in a profile. Its numbers are only comparable when the host has not already been
# loaded by an hour of other tests, which is why run.sh places it directly after the throughput reference.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,9p' "$0"; usage_common; exit 0; }
: "${PHASE_S:=$(q 30 15)}" "${LOADED_P95_MAX:=1000}" "${PARALLEL:=1 3 6}"
suite_init; require_target
connect "$DEST" 15 || { verdict T05-loaded-latency FAIL "connect failed"; exit 1; }
ping_phase() { in_client "ping -c $PHASE_S -i 1 -W 3 $TARGET_IP 2>/dev/null | grep -oE 'time=[0-9.]+' | cut -d= -f2" | tr '\n' ' '; }
idle=$(ping_phase)
in_client_bg "curl -s -o /dev/null -m $((PHASE_S+5)) http://$TARGET_IP:8899/down?bytes=400000000"; sleep 2; dl=$(ping_phase); sleep 5
in_client "head -c 200000000 /dev/zero > /tmp/up200.bin"; in_client_bg "curl -s -o /dev/null -m $((PHASE_S+5)) --data-binary @/tmp/up200.bin http://$TARGET_IP:8899/up"; sleep 2; ul=$(ping_phase); sleep 5
is=$(stats_json $idle); ds=$(stats_json $dl); us=$(stats_json $ul)
p95() { python3 -c 'import sys; v=sorted(float(x) for x in sys.argv[1:]); print(v[min(len(v)-1,int(0.95*len(v)))] if v else 0)' $1; }
emit_row T05-loaded-latency phase=idle "rtt=$is" p95="$(p95 "$idle")"; emit_row T05-loaded-latency phase=download "rtt=$ds" p95="$(p95 "$dl")"; emit_row T05-loaded-latency phase=upload "rtt=$us" p95="$(p95 "$ul")"
agg_ok=1; prev=0
for n in $PARALLEL; do
  t0=$(now_ms); in_client "for i in \$(seq 1 $n); do curl -s -o /dev/null -m $CAP http://$TARGET_IP:8899/down?bytes=$BYTES & done; wait"; dt=$(( $(now_ms) - t0 ))
  agg=$(python3 -c "print(round($n*$BYTES*8/($dt/1000)/1e6,2))"); emit_row T05-loaded-latency parallel="$n" dir=down agg_mbit="$agg" elapsed_ms="$dt"
  python3 -c "import sys; sys.exit(0 if $agg >= 0.8*$prev else 1)" || agg_ok=0; prev=$agg; suite_log "parallel $n downloads: $agg Mbit/s aggregate"
done
disconnect
dp=$(p95 "$dl"); up=$(p95 "$ul")
msg="idle p50 $(json_get "$is" median) ms; download p95 ${dp} ms; upload p95 ${up} ms; parallel aggregate non-decreasing=$agg_ok"
record T05-loaded-latency "$msg"
if [ "$agg_ok" = 1 ]; then
  verdict T05-loaded-latency PASS "parallel aggregate does not fall as N rises over [$PARALLEL]"
else
  verdict T05-loaded-latency FAIL "parallel aggregate fell as N rose over [$PARALLEL] — parallel flows are not scaling; see the parallel rows"
fi
score_delta T05-loaded-latency loaded_rtt_p95_download_ms "${dp:-0}" lower_better
score_delta T05-loaded-latency loaded_rtt_p95_upload_ms   "${up:-0}" lower_better
exit $SUITE_FAILED
