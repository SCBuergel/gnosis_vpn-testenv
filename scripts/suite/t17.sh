#!/usr/bin/env bash
# T17-latency-matrix — Node-to-node latency matrix. N hoprd pings per ordered node pair (min/median/mean/max/stdev) plus a
# peer survey from node 0. On a single host the absolute medians are ~2-3 ms and say little, so this is a
# control — except when the cell injected an impairment map (SUITE_LATENCY_MAP="idx=ms ..."), in which case it
# is a gate: the measured pair medians must match the configured map. A silently-inert latency flag would
# otherwise poison every rung of the impairment test above it.
source "$(dirname "$0")/lib.sh"
suite_kind diagnostic
[ "${1:-}" = "--help" ] && { sed -n '2,7p' "$0"; usage_common; exit 0; }
: "${N:=10}" "${STDEV_MAX:=25}" "${SUITE_LATENCY_MAP:=}" "${LATENCY_TOL_MS:=15}"
suite_init
declare -A MED
for i in $(seq 0 $((CLUSTER_SIZE-1))); do for j in $(seq 0 $((CLUSTER_SIZE-1))); do
  [ "$i" = "$j" ] && continue; v=()
  for k in $(seq 1 "$N"); do v+=("$(node_ping "$i" "$j")"); sleep 0.2; done
  st=$(stats_json "${v[@]}"); emit_row T17-latency-matrix from="$i" to="$j" "stats=$st"
  MED[$i-$j]=$(json_get "$st" median); suite_log "node$i->node$j median ${MED[$i-$j]} ms stdev $(json_get "$st" stdev)"
done; done
peers=$(node_curl 0 GET /api/v4/network/connected | python3 -c 'import sys,json
d=json.load(sys.stdin); l=d if isinstance(d,list) else d.get("connected",[])
print(json.dumps([{"address":p.get("address"),"avg_ms":p.get("averageLatency"),"probe_rate":p.get("probeRate")} for p in l]))' 2>/dev/null || echo '[]')
emit_row T17-latency-matrix kind=peer_survey from=0 "peers=$peers" "latency_map=\"$SUITE_LATENCY_MAP\""
if [ -z "$SUITE_LATENCY_MAP" ]; then
  record T17-latency-matrix "no impairment configured: $(( CLUSTER_SIZE*(CLUSTER_SIZE-1) )) pairs, medians $(for k in "${!MED[@]}"; do printf '%s ' "${MED[$k]}"; done)"
  exit 0
fi
# gate: every configured delay must show up in the measured medians (traffic to that node, from node 0)
suite_kind gate; bad=""
for kv in $SUITE_LATENCY_MAP; do
  idx=${kv%=*}; want=${kv#*=}; got=${MED[0-$idx]:-}
  [ -z "$got" ] && { bad="$bad node$idx:no-measurement"; continue; }
  python3 -c "import sys; sys.exit(0 if abs($got - ($want + ${MED[0-1]:-3})) <= $LATENCY_TOL_MS or $got >= $want else 1)" \
    || bad="$bad node$idx:want>=${want}ms,got${got}ms"
done
[ -z "$bad" ] && verdict T17-latency-matrix PASS "impairment verified: map '$SUITE_LATENCY_MAP' visible in the measured pair medians" \
              || verdict T17-latency-matrix FAIL "impairment NOT applied:$bad (map '$SUITE_LATENCY_MAP') — rungs above this are meaningless"
exit $SUITE_FAILED
