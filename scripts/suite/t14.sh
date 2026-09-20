#!/usr/bin/env bash
# T14-novpn-baseline — No-VPN baseline: the same transfers from the client container straight to the target (no tunnel).
# Pass: all REPS downloads and uploads complete (a diagnostic, so a miss is WARN); rate and RTT are recorded.
source "$(dirname "$0")/lib.sh"
suite_kind diagnostic
[ "${1:-}" = "--help" ] && { sed -n '2,4p' "$0"; usage_common; exit 0; }
suite_init; require_target
if client_is_connected; then disconnect; fi
WG_IFACE=eth0
summary=$(transfer_series baseline "$TARGET_IP_DIRECT" "$REPS" "$BYTES" "$CAP" T14-novpn-baseline)
rtt=$(in_client "ping -c 5 -i 0.2 -W 2 $TARGET_IP_DIRECT 2>/dev/null | sed -n 's|.*= \([0-9.]*\)/\([0-9.]*\)/.*|\2|p'" || true)
emit_row T14-novpn-baseline kind=summary "summary=$summary" rtt_ms="${rtt:-null}"
dc=$(json_get "$summary" down_complete); uc=$(json_get "$summary" up_complete)
msg="down $(json_get "$summary" down_median) up $(json_get "$summary" up_median) Mbit/s, complete $dc/$REPS + $uc/$REPS, rtt ${rtt:-?} ms"
[ "$dc" -eq "$REPS" ] && [ "$uc" -eq "$REPS" ] && verdict T14-novpn-baseline PASS "$msg" || verdict T14-novpn-baseline FAIL "$msg"
exit $SUITE_FAILED
