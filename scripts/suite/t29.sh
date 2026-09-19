#!/usr/bin/env bash
# T29-destination-sweep — Multi-destination sweep: for every destination the client reports: connect, in-tunnel ping, one download,
# disconnect; CYCLES rounds. Pass: every destination connects in every cycle.
source "$(dirname "$0")/lib.sh"
suite_kind runbook
[ "${1:-}" = "--help" ] && { sed -n '2,4p' "$0"; usage_common; exit 0; }
: "${CYCLES:=$(q 3 1)}"
suite_init; require_target; fail=0
dests=$(client_status_text | sed -n 's/^\([^ ]*\) Route health:.*/\1/p')
for c in $(seq 1 "$CYCLES"); do for d in $dests; do
  if connect "$d" 5; then
    rtt=$(in_client "ping -c 3 -i 0.3 -W 3 $TARGET_IP 2>/dev/null | sed -n 's|.*= \([0-9.]*\)/\([0-9.]*\)/.*|\2|p'" || true); r=$(curl_down "$TARGET_IP" "$BYTES" "$CAP"); disconnect
    emit_row T29-destination-sweep cycle="$c" dest="$d" connect=true connect_ms="$CONNECT_MS" rtt_ms="${rtt:-null}" "download=$r"
    verdict T29-destination-sweep PASS "$d cycle $c: connect ${CONNECT_MS} ms, rtt ${rtt:-?} ms, down $(json_get "$r" mbit) Mbit/s"
  else emit_row T29-destination-sweep cycle="$c" dest="$d" connect=false; verdict T29-destination-sweep FAIL "$d cycle $c: connect failed"; fail=1; fi
done; done
exit $fail
