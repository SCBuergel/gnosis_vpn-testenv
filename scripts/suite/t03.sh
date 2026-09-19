#!/usr/bin/env bash
# T03-repeatability-baseline — Repeatability baseline (PREREQUISITE, kind: gate-support). N unchanged warm T04-fixed-throughput cells back to back on
# one stack; reports spread and writes the band this stack's delta scoring uses (SUITE_BANDS_DIR/<stack_key>.json).
# Until a stack has this record, run.sh records host-dependent numbers instead of scoring them.
# Env: N (default 10, fast 5). No pass/fail of its own — it produces the threshold every other test is judged by.
source "$(dirname "$0")/lib.sh"
suite_kind diagnostic
[ "${1:-}" = "--help" ] && { sed -n '2,5p' "$0"; usage_common; exit 0; }
: "${N:=$(q 10 5)}"
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
band_write "$band"
emit_row T03-repeatability-baseline kind=summary "down=$ds" "up=$us" "band=$band"
mde=$(json_get "$band" mde_pct)
record T03-repeatability-baseline "n=$N warm on stack $(stack_key): down median $(json_get "$ds" median) stdev $(json_get "$ds" stdev); up median $(json_get "$us" median) stdev $(json_get "$us" stdev); band +-${mde}% at REPS=$REPS"
# A band this wide is a finding about the stack, not a licence to ignore regressions. Say so here as well as at
# the point of use: on the 2026-09-17 old-version run the band came out at +-279 % and every delta gate passed.
if python3 -c "import sys; sys.exit(0 if float('${mde:-0}') > float('$BAND_MAX_PCT') else 1)"; then
  record T03-repeatability-baseline "UNSTABLE BASELINE: measured band +-${mde}% exceeds BAND_MAX_PCT ${BAND_MAX_PCT}% — this stack cannot repeat its own throughput, so delta scoring is clamped to +-${BAND_MAX_PCT}% and every delta result this run is weak evidence. Treat absolute assertions as the real signal."
fi
exit 0
