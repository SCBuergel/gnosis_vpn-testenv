#!/usr/bin/env bash
# T13-mtu-sweep — Tunnel MTU / datagram-size sweep. MTU is not a tuning knob here, it is a mechanism switch: at <=940 B a
# datagram fits one HOPR packet so no opportunistic second SURB is minted, and the sustained-upload death
# disappears at identical throughput.
# Until 2026-09-20 this was an EXPECTED FAIL tagged with hoprnet#8392 (gate organic SURB production on the balancer
# target): reconnects at MTU 1420 and 1280, clean at 940. On hoprd 4.1.3 (release/4.1 @ 60269a3) fullrun5 recorded
# XPASS (no reconnect at any MTU) and T24-sustained-upload ran 900 s at MTU 1420 with 0.0 % loss, so the overflow is not
# in the tested stack and this is now the plain gate the XFAIL was waiting to become: zero reconnects at every MTU.
# FIXED_BY is kept as the name of the mechanism, for the day a reconnect at 1420/1280 with 940 clean comes back.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,9p' "$0"; usage_common; exit 0; }
: "${MTUS:=1420 1280 940}" "${STREAM_S:=$(q 120 120)}" "${FIXED_BY:=hoprnet#8392}" "${MTU940_DOWN_MIN_MBIT:=6}"
suite_init; require_target
declare -A REC
for mtu in $MTUS; do
  connect "$DEST" 15 || { verdict T13-mtu-sweep FAIL "connect failed at mtu $mtu"; exit 1; }
  [ "$mtu" != default ] && in_client "ip link set dev $WG_IFACE mtu $mtu"; eff=$(in_client "cat /sys/class/net/$WG_IFACE/mtu")
  s=$(transfer_series "t13-$mtu" "$TARGET_IP" "$(q "$REPS" 2)" "$BYTES" "$CAP" T13-mtu-sweep)
  in_client "python3 /suite/probes/streamprobe.py --mode ul --host $TARGET_IP --port 8902 --rate-mbit 3 --duration $STREAM_S --size 1200 --iface $WG_IFACE --out ${SUITE_RUN_IN_CLIENT}/t13-$mtu.json" >/dev/null 2>&1 || true
  j=$(cat "$SUITE_RUN/t13-$mtu.json" 2>/dev/null || echo '{}'); e=$(log_errors "$LOG_SINCE"); disconnect
  REC[$mtu]=$(json_get "$e" reconnects)
  emit_row T13-mtu-sweep mtu="$mtu" effective_mtu="$eff" "summary=$s" "stream=$j" "errors=$e"
  record T13-mtu-sweep "mtu $eff: down $(json_get "$s" down_median) up $(json_get "$s" up_median) Mbit/s, stream loss $(json_get "$j" loss_pct)%, reconnects ${REC[$mtu]} (tunnel-ping timeouts $(json_get "$e" ping_timeouts))"
done
big=$(( ${REC[1420]:-0} + ${REC[1280]:-0} )); small=${REC[940]:-0}
# throughput must not depend on MTU: the point is the same speed with a different failure mode. The 940 rung's
# download median is held against MTU940_DOWN_MIN_MBIT (6): it measured 12.3 Mbit/s on the reference stack, and 6
# sits under T04's floor of 7 because a 940 B MTU carries about a third more packets per byte.
m940=$(grep -a '"test": "T13-mtu-sweep"' "$SUITE_RUN/rows.jsonl" | python3 -c 'import sys,json
best=0
for l in sys.stdin:
    try:
        r=json.loads(l)
        if str(r.get("mtu"))=="940" and isinstance(r.get("summary"),dict): best=r["summary"].get("down_median") or 0
    except Exception: pass
print(best)' 2>/dev/null || echo 0)
[ "${m940:-0}" != 0 ] && assert_min T13-mtu-sweep "download median at mtu 940" "$m940" Mbit/s MTU940_DOWN_MIN_MBIT
if [ "$small" -eq 0 ] && [ "$big" -eq 0 ]; then
  verdict T13-mtu-sweep PASS "no reconnect at any mtu [$MTUS]"
elif [ "$small" -eq 0 ] && [ "$big" -gt 0 ]; then
  verdict T13-mtu-sweep FAIL "organic-SURB overflow is back ($FIXED_BY): reconnects at 1420/1280 (${REC[1420]:-0}/${REC[1280]:-0}), clean at 940"
else
  verdict T13-mtu-sweep FAIL "reconnects 1420=${REC[1420]:-0} 1280=${REC[1280]:-0} 940=${small}; 940 must be clean, so this is not the known mechanism"
fi
exit $SUITE_FAILED
