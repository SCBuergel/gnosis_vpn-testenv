#!/usr/bin/env bash
# T10-forced-reconnect — Forced reconnect under live load: during a call (callprobe → :8903) the WireGuard peer is removed on the
# exit server container at +T_KILL s; arm T = far end keeps streaming, arm S = far end pauses while we are silent.
# Records time to recovery (first downstream packet after the kill), DecapStalled, rebinds. Env: DUR (300; fast 150).
# Pass: recovery within RECOVER_MAX s (90) in both arms, zero DecapStalled.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,6p' "$0"; usage_common; exit 0; }
: "${DUR:=$(q 300 150)}" "${T_KILL:=60}" "${RECOVER_MAX:=90}" "${REPEATS:=$(q 3 1)}"
suite_init; require_target; fail=0
for arm in T S; do for rep in $(seq 1 "$REPEATS"); do
  connect "$DEST" 15 || { verdict T10-forced-reconnect FAIL "connect failed"; exit 1; }
  sid=$RANDOM$RANDOM; extra=""; [ "$arm" = S ] && extra="--idle-pause"
  in_client_bg "python3 /suite/probes/callprobe.py --host $TARGET_IP --port 8903 --sid $sid --duration $DUR --iface $WG_IFACE --out ${SUITE_RUN_IN_CLIENT}/t10-$arm-$rep $extra"
  sleep "$T_KILL"
  pub=$(in_client "wg show $WG_IFACE public-key" 2>/dev/null || true)
  # remove our peer on the server so the tunnel dies from the outside
  tkill=$(date +%s.%N); docker exec gnosis_vpn-server-0 sh -c "wg show wggvpn peers | while read p; do wg set wggvpn peer \$p remove; done" 2>/dev/null || true
  sleep $((DUR - T_KILL + 20))
  e=$(log_errors "$LOG_SINCE"); save_client_log "t10-$arm-$rep" "$LOG_SINCE"; disconnect
  rec=$(python3 - "$SUITE_RUN/t10-$arm-$rep.csv" "$tkill" <<'PY'
import sys
tk=float(sys.argv[2]); first=None; last_before=None
try:
    for line in open(sys.argv[1]):
        p=line.strip().split(",")
        if p[0]=="R":
            t=float(p[-1])
            if t<tk: last_before=t
            elif first is None and t>tk+2: first=t
except FileNotFoundError: pass
print(round(first-tk,1) if first else "never")
PY
)
  j=$(cat "$SUITE_RUN/t10-$arm-$rep.json" 2>/dev/null || echo '{}')
  emit_row T10-forced-reconnect arm="$arm" rep="$rep" recovery_s="$rec" "errors=$e" "call=$j"
  # "never" is a word, not a duration: "recovered nevers after the kill" was the 2026-09-17 output
  if [ "$rec" = never ]; then recd="NEVER recovered"; else recd="recovered ${rec}s after the kill"; fi
  msg="arm $arm rep $rep: downstream ${recd}; DecapStalled $(json_get "$e" decap_stalled); reconnects $(json_get "$e" reconnects); rebinds $(json_get "$j" rebinds)"
  python3 -c "import sys; r='$rec'; sys.exit(0 if r!='never' and float(r)<=$RECOVER_MAX and $(json_get "$e" decap_stalled)==0 else 1)" && verdict T10-forced-reconnect PASS "$msg" || { verdict T10-forced-reconnect FAIL "$msg"; fail=1; }
done; done
exit $fail
