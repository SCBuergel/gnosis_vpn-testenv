#!/usr/bin/env bash
# T18-capacity-ceiling — Exit and relay capacity ceiling (diagnostic; absorbs the former watchdog-under-saturation test as its top rung): download-only UDP rate ladder until delivery collapses, node CPU sampled.
# Env: LADDER="1 2 4 8 12 16" (Mbit/s) STEP_S (60; fast 30). Reports knee (last rate with loss < 5 %) and CPU per node.
source "$(dirname "$0")/lib.sh"
suite_kind diagnostic
[ "${1:-}" = "--help" ] && { sed -n '2,4p' "$0"; usage_common; exit 0; }
: "${LADDER:=$(q "1 2 4 8 12 16" "2 4 8 12")}" "${STEP_S:=$(q 60 30)}"
suite_init; require_target
connect "$DEST" 15 || { verdict T18-capacity-ceiling FAIL "connect failed"; exit 1; }
knee=0
for r in $LADDER; do
  node_sampler_start "t18-$r" 1
  in_client "python3 /suite/probes/streamprobe.py --mode dl --host $TARGET_IP --port 8902 --rate-mbit $r --duration $STEP_S --size 1200 --iface $WG_IFACE --out ${SUITE_RUN_IN_CLIENT}/t18-$r.json" >/dev/null 2>&1 || true
  node_sampler_stop; j=$(cat "$SUITE_RUN/t18-$r.json" 2>/dev/null || echo '{"loss_pct":100}')
  cpu=$(python3 -c 'import sys,json
rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
import statistics as st
ks=set(k for r in rows for k in r.get("cpu_pct",{})); print(json.dumps({k: round(st.mean([r["cpu_pct"][k] for r in rows if k in r.get("cpu_pct",{})]),1) for k in ks}))' "$SUITE_RUN/samples/t18-$r.jsonl" 2>/dev/null || echo '{}')
  loss=$(json_get "$j" loss_pct); e=$(log_errors "$LOG_SINCE"); rec=$(json_get "$e" reconnects)
  emit_row T18-capacity-ceiling rate_mbit="$r" "result=$j" "node_cpu_pct=$cpu" "errors=$e"
  # former watchdog test: below the knee the tunnel watchdog must not fire; above it, record how it tears down
  if python3 -c "import sys; sys.exit(0 if ${loss:-100} < 5 else 1)"; then
    [ "${rec:-0}" -gt 0 ] && record T18-capacity-ceiling "watchdog fired ${rec}x at ${r} Mbit/s while loss was ${loss}% (below the knee)"
  else
    wd_above=$(( ${wd_above:-0} + ${rec:-0} ))
  fi
  suite_log "rate $r Mbit/s: loss ${loss}% p99 $(json_get "$j" delay_over_min_ms.p99) ms cpu $cpu"
  python3 -c "import sys; sys.exit(0 if ${loss:-100} < 5 else 1)" && knee=$r
  sleep 5
done
disconnect; emit_row T18-capacity-ceiling kind=summary knee_mbit="$knee" watchdog_reconnects_above_knee="${wd_above:-0}"
record T18-capacity-ceiling "ceiling: last clean rung ${knee} Mbit/s of ladder [$LADDER]; watchdog reconnects above the knee: ${wd_above:-0} (see rows for per-node CPU)"
exit 0
