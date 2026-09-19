#!/usr/bin/env bash
# T30-hopcount-ab — Hop-count A/B on one exit: T04-fixed-throughput at 1 hop (DEST) and 0 hops (DEST-h0). Needs HOPS0_ALSO=1 at gen-config and
# CLIENT_EXTRA_ARGS=--allow-insecure at client start; SKIPs otherwise. Pass: 0-hop is the upper bound.
source "$(dirname "$0")/lib.sh"
suite_kind runbook
[ "${1:-}" = "--help" ] && { sed -n '2,4p' "$0"; usage_common; exit 0; }
suite_init; require_target
dest_health_line "${DEST}-h0" | grep -q . || { verdict T30-hopcount-ab SKIP "no 0-hop destination ${DEST}-h0 (set HOPS0_ALSO=1 CLIENT_EXTRA_ARGS=--allow-insecure)"; exit 0; }
res=()
for d in "$DEST" "${DEST}-h0"; do
  connect "$d" 15 || { verdict T30-hopcount-ab FAIL "connect $d failed"; continue; }
  s=$(transfer_series "t30-$d" "$TARGET_IP" "$(q "$REPS" 2)" "$BYTES" "$CAP" T30-hopcount-ab); e=$(log_errors "$LOG_SINCE"); disconnect
  emit_row T30-hopcount-ab dest="$d" "summary=$s" "errors=$e"; res+=("$(json_get "$s" down_median)")
done
[ "${#res[@]}" -eq 2 ] || exit $SUITE_FAILED
python3 -c "import sys; sys.exit(0 if ${res[1]} >= ${res[0]} else 1)" && verdict T30-hopcount-ab PASS "1-hop down ${res[0]} Mbit/s, 0-hop down ${res[1]} Mbit/s (0-hop is the upper bound)" || verdict T30-hopcount-ab WARN "0-hop down ${res[1]} < 1-hop ${res[0]} Mbit/s"
exit $SUITE_FAILED
