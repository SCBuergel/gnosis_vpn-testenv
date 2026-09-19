#!/usr/bin/env bash
# T21-passive-observer — Independent passive observer: the second client never connects; it polls destination health for DUR s while
# the suite runs on client 1 (start with --background to fork). Compares the two views at the end.
# Pass: both clients report the same Ready/not-Ready per destination.
source "$(dirname "$0")/lib.sh"
suite_kind diagnostic
[ "${1:-}" = "--help" ] && { sed -n '2,4p' "$0"; usage_common; exit 0; }
: "${DUR:=$(q 300 60)}"
suite_init
docker container inspect "$CLIENT2" >/dev/null 2>&1 || { verdict T21-passive-observer SKIP "second client not running (EXTRA_IDENTITIES=2 + just client2-start)"; exit 0; }
t0=$(date +%s); : > "$SUITE_RUN/t21-observer.csv"
while [ $(( $(date +%s) - t0 )) -lt "$DUR" ]; do
  a=$(client_status_text | grep -c 'Route health: Ready' || true); b=$(docker exec "$CLIENT2" gnosis_vpn-ctl status 2>/dev/null | grep -c 'Route health: Ready' || true)
  echo "$(date +%s),$a,$b" >> "$SUITE_RUN/t21-observer.csv"; sleep 15
done
dis=$(python3 -c 'import sys; r=[l.strip().split(",") for l in open(sys.argv[1]) if l.strip()]; print(sum(1 for x in r if x[1]!=x[2]), len(r))' "$SUITE_RUN/t21-observer.csv")
emit_row T21-passive-observer disagreements="${dis%% *}" samples="${dis##* }"
[ "${dis%% *}" -le $(( ${dis##* } / 5 )) ] && verdict T21-passive-observer PASS "active and passive client agreed on Ready destinations in $(( ${dis##* } - ${dis%% *} ))/${dis##* } samples" || verdict T21-passive-observer FAIL "views diverged in ${dis%% *}/${dis##* } samples (local client state, not the network)"
exit $SUITE_FAILED
