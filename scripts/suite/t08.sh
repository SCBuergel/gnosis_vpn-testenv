#!/usr/bin/env bash
# T08-relay-attribution — Return-path relay attribution. Parses the client's path-planner lines during a download and counts
# resolved return paths per first-hop relay (the 20-byte address inside path=[...], not the destination field).
# Two parts:
#   membership (gate)  every return path's first hop is an open outgoing channel of the exit
#   split      (gate only when the relays carry equal injected latency, diagnostic otherwise) — on equal links
#              only a near-dead relay is flagged. The split legitimately varies a lot by version (51/49 to
#              85/15 observed across releases), so SPLIT_TOL_PCT is 60 %, not near-even: it passes ordinary
#              planner weighting and fails only when one relay carries under ~20 % of return paths. The real
#              correctness assertion here is membership, which is absolute; the split is a coarse guard.
# Set SUITE_EQUAL_LATENCY=1 when the cell's impairment map gives every return relay the same delay.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,9p' "$0"; usage_common; exit 0; }
: "${SUITE_EQUAL_LATENCY:=1}" "${SPLIT_TOL_PCT:=60}"
suite_init; require_target
connect "$DEST" 10 || { verdict T08-relay-attribution FAIL "connect failed"; exit 1; }
r=$(curl_down "$TARGET_IP" "$BYTES" "$CAP")
split=$(docker logs --since "$LOG_SINCE" "$CLIENT" 2>&1 | python3 -c '
import sys,re,json,collections
ansi=re.compile(r"\x1b\[[0-9;]*m"); c=collections.Counter(); n=0
for line in sys.stdin:
    line=ansi.sub("",line)
    if "resolved return path" not in line: continue
    m=re.search(r"path=validated path \[([^\]]*)\]", line)
    if not m: continue
    hops=[h.strip() for h in m.group(1).split(",")]
    if len(hops)>=2: c[hops[0].lower()]+=1; n+=1
print(json.dumps({"total":n,"by_relay":dict(c)}))'); disconnect
exits=$(node_curl 0 GET /api/v4/channels | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps([c["peerAddress"].lower() for c in d.get("outgoing",[]) if c.get("status")=="Open"]+[sys.argv[1].lower()]))' "$(node_addr 0)" 2>/dev/null || echo '[]')
emit_row T08-relay-attribution "split=$split" "exit_open_outgoing=$exits" "download=$r" equal_latency="$SUITE_EQUAL_LATENCY"
tot=$(json_get "$split" total)
[ "${tot:-0}" -eq 0 ] && { verdict T08-relay-attribution WARN "no 'resolved return path' lines (is hopr_transport::path=debug in CLIENT_LOG_LEVEL?)"; exit 0; }
# part 1 — membership (always a gate)
if python3 -c 'import sys,json; s=json.loads(sys.argv[1]); ex=set(json.loads(sys.argv[2])); sys.exit(1 if [k for k in s["by_relay"] if k not in ex] else 0)' "$split" "$exits"; then
  verdict T08-relay-attribution PASS "membership: all $tot return paths start at an open outgoing channel of the exit — $(json_get "$split" by_relay)"
else
  verdict T08-relay-attribution FAIL "membership: a return relay is outside the exit's open channel set: $(json_get "$split" by_relay) vs $exits"
fi
# part 2 — split, scored only when the relays are at equal latency
sk=$(python3 -c 'import sys,json
s=json.loads(sys.argv[1])["by_relay"]; me=sys.argv[2].lower()
v=sorted((n for k,n in s.items() if k!=me), reverse=True)
print(json.dumps({"relays":len(v),"skew_pct": round(100*(v[0]-v[-1])/max(sum(v),1),1) if len(v)>1 else None,"counts":v}))' "$split" "$(node_addr 0)")
n_relays=$(json_get "$sk" relays); skew=$(json_get "$sk" skew_pct)
if [ "${n_relays:-0}" -lt 2 ] || [ -z "$skew" ] || [ "$skew" = None ]; then
  record T08-relay-attribution "split: only ${n_relays:-0} return relay(s) in play, distribution not meaningful"
elif [ "$SUITE_EQUAL_LATENCY" = 1 ]; then
  python3 -c "import sys; sys.exit(0 if $skew <= $SPLIT_TOL_PCT else 1)" \
    && verdict T08-relay-attribution PASS "split on equal-latency relays: skew ${skew}% <= ${SPLIT_TOL_PCT}% $(json_get "$sk" counts)" \
    || verdict T08-relay-attribution FAIL "split on equal-latency relays skewed ${skew}% > ${SPLIT_TOL_PCT}% $(json_get "$sk" counts) — planner behaviour change"
else
  record T08-relay-attribution "split under unequal latency (diagnostic): skew ${skew}% $(json_get "$sk" counts)"
fi
exit $SUITE_FAILED
