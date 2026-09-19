#!/usr/bin/env bash
# T15-warmup-knee — Post-connect warm-up knee (diagnostic, not a gate). First-transfer throughput vs idle time after connect.
# A fresh session ALWAYS warms up: the exit's shaper starts at its initial egress rate and the readiness gate
# has to clear, so "flat across the sweep" is not a healthy-stack property and was wrong as a gate (it failed
# even on ramp-off stacks that pass T07-cold-start). What this measures instead is the KNEE — the idle time after which
# the first transfer reaches full rate. T07-cold-start is the gate; this number tells you how long the warm-up lasts and
# which constant it matches. Env: DELAYS="0 5 15 30 60".
source "$(dirname "$0")/lib.sh"
suite_kind diagnostic
[ "${1:-}" = "--help" ] && { sed -n '2,4p' "$0"; usage_common; exit 0; }
: "${DELAYS:=$(q "0 5 15 30 60" "0 5 20")}"
suite_init; require_target
vals=()
for d in $DELAYS; do
  # this test IS the delay sweep, so it opts out of the global ramp wait
  RAMP_WAIT_OPT_OUT=1 connect "$DEST" "$d" || { verdict T15-warmup-knee FAIL "connect failed at delay $d"; continue; }
  persec_start "t15-$d"; r=$(curl_down "$TARGET_IP" "$BYTES" "$CAP"); persec_stop
  e=$(log_errors "$LOG_SINCE"); disconnect
  emit_row T15-warmup-knee delay="$d" "first_download=$r" "errors=$e" stall_s="$(persec_stall "t15-$d" rx)"
  vals+=("$(json_get "$r" mbit)"); suite_log "delay $d s -> $(json_get "$r" mbit) Mbit/s complete=$(json_get "$r" complete)"
done
st=$(stats_json "${vals[@]}")
# knee: the first delay at which the first transfer reaches KNEE_FRAC of the best rung
knee=$(python3 -c 'import sys
ds=sys.argv[1].split(); vs=[float(x) for x in sys.argv[2].split()]; frac=float(sys.argv[3])
best=max(vs) if vs else 0
print(next((d for d,v in zip(ds,vs) if best>0 and v>=frac*best), "beyond-sweep"))' "$DELAYS" "${vals[*]}" "${KNEE_FRAC:-0.8}")
emit_row T15-warmup-knee kind=summary "stats=$st" delays="\"$DELAYS\"" knee_s="$knee" first_transfer_mbit="\"${vals[*]}\""
record T15-warmup-knee "warm-up knee at ${knee}s idle (first transfer reaches ${KNEE_FRAC:-0.8} of best); Mbit/s over delays [$DELAYS]: ${vals[*]}"
python3 -c "import sys; k='$knee'; sys.exit(0 if k!='beyond-sweep' and float(k)<=${KNEE_MAX_S:-30} else 1)" 2>/dev/null \
  || record T15-warmup-knee "knee is beyond ${KNEE_MAX_S:-30}s — a warm-up that long is a ramp defect, compare against T07-cold-start"
exit $SUITE_FAILED
