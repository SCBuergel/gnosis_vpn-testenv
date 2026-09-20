#!/usr/bin/env bash
# T03-repeatability-baseline — Repeatability baseline (diagnostic). N unchanged warm T04-fixed-throughput cells back to back on
# one stack; records medians, spread and the minimum detectable effect at REPS transfers (mde_pct), so the headroom of
# the suite's absolute thresholds can be judged for this stack. Flags UNSTABLE above UNSTABLE_PCT.
# Env: N (default 10, fast 5) UNSTABLE_PCT (50). No pass/fail of its own.
source "$(dirname "$0")/lib.sh"
suite_kind diagnostic
[ "${1:-}" = "--help" ] && { sed -n '2,5p' "$0"; usage_common; exit 0; }
: "${N:=$(q 10 5)}" "${UNSTABLE_PCT:=50}"
suite_init; require_target
d=(); u=()
for i in $(seq 1 "$N"); do
  connect "$DEST" 20 || { record T03-repeatability-baseline "rep $i: connect failed"; continue; }
  r=$(curl_down "$TARGET_IP" "$BYTES" "$CAP"); w=$(curl_up "$TARGET_IP" "$BYTES" "$CAP"); disconnect
  emit_row T03-repeatability-baseline rep="$i" "down=$r" "up=$w"; d+=("$(json_get "$r" mbit)"); u+=("$(json_get "$w" mbit)")
done
ds=$(stats_json "${d[@]}"); us=$(stats_json "${u[@]}")
band=$(python3 -c 'import sys,json
ds=json.loads(sys.argv[1]); us=json.loads(sys.argv[2]); reps=int(sys.argv[3])
def mde(s):
    if not s.get("n") or not s.get("mean"): return None
    return round(2*1.96*s["stdev"]/max(s["mean"],1e-9)/ (reps**0.5) * 100, 1)
m=[x for x in (mde(ds), mde(us)) if x is not None]
print(json.dumps({"n":ds.get("n",0),"down_median":ds.get("median"),"down_stdev":ds.get("stdev"),
 "up_median":us.get("median"),"up_stdev":us.get("stdev"),
 "mde_pct":max(m) if m else 20, "reps_assumed":reps}))' "$ds" "$us" "$REPS")
emit_row T03-repeatability-baseline kind=summary "down=$ds" "up=$us" "band=$band"
mde=$(json_get "$band" mde_pct)
record T03-repeatability-baseline "n=$N warm on stack $(stack_key): down median $(json_get "$ds" median) stdev $(json_get "$ds" stdev); up median $(json_get "$us" median) stdev $(json_get "$us" stdev); minimum detectable effect +-${mde}% at REPS=$REPS"
# A spread this wide is a finding about the stack: the 2026-09-17 old-version run measured +-279 %, and no
# threshold with sane headroom can be trusted on a stack that cannot repeat its own numbers.
if python3 -c "import sys; sys.exit(0 if float('${mde:-0}') > float('$UNSTABLE_PCT') else 1)"; then
  record T03-repeatability-baseline "UNSTABLE BASELINE: minimum detectable effect +-${mde}% exceeds UNSTABLE_PCT ${UNSTABLE_PCT}%; this stack cannot repeat its own throughput, so read every threshold verdict in this run as weak evidence"
fi
exit 0
