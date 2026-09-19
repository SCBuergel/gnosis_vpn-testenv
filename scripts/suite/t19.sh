#!/usr/bin/env bash
# T19-background-load — Contending background load (trickle): client 1 runs T04-fixed-throughput while client 2 is idle (control), then fetches
# TRICKLE_KB every 2 s through the same exit. Pass: degradation proportional to bytes; a step change is a finding.
source "$(dirname "$0")/lib.sh"
suite_kind diagnostic
[ "${1:-}" = "--help" ] && { sed -n '2,4p' "$0"; usage_common; exit 0; }
: "${TRICKLES:=10 100}"
suite_init; require_target
docker container inspect "$CLIENT2" >/dev/null 2>&1 || { verdict T19-background-load SKIP "second client not running (EXTRA_IDENTITIES=2 + just client2-start)"; exit 0; }
CLIENT="$CLIENT2" bash -c "source '$SUITE_LIB_DIR/lib.sh'; connect '$DEST' 15" || { verdict T19-background-load FAIL "client 2 connect failed"; exit 1; }
connect "$DEST" 15 || { verdict T19-background-load FAIL "client 1 connect failed"; exit 1; }
ctrl=$(curl_down "$TARGET_IP" "$BYTES" "$CAP"); emit_row T19-background-load arm=idle-neighbour "download=$ctrl"; res="idle $(json_get "$ctrl" mbit)"
for kb in $TRICKLES; do
  docker exec -d "$CLIENT2" sh -c "for i in \$(seq 1 120); do curl -s -o /dev/null -m 10 http://$TARGET_IP:8899/down?bytes=$((kb*1000)); sleep 2; done"
  sleep 4; r=$(curl_down "$TARGET_IP" "$BYTES" "$CAP"); docker exec "$CLIENT2" sh -c 'pkill -f "seq 1 120"; pkill curl' >/dev/null 2>&1 || true
  emit_row T19-background-load arm="trickle-${kb}kB" "download=$r"; res="$res; ${kb}kB/2s $(json_get "$r" mbit)"
done
disconnect; docker exec "$CLIENT2" gnosis_vpn-ctl disconnect >/dev/null 2>&1 || true
verdict T19-background-load PASS "client-1 download Mbit/s with neighbour: $res"
exit 0
