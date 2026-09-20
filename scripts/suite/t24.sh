#!/usr/bin/env bash
# T24-sustained-upload — Sustained one-directional upload (organic-SURB overflow guard): upload-only stream at RATE Mbit/s for DUR s
# with full-size datagrams, sampling the client's undecodable counter; then the same with MTU 940.
# Pass: stream completes without reconnect, undecodable flat, loss < 5 %. Env: RATE=3 DUR=900 (fast 240).
# The session must outlive the deadman: deadman_cover raises DEADMAN above DUR (fullrun5 ran 900 s arms under the
# 900 s default, the deadman disconnected the client ~30 s before the sender finished, the end-of-stream report
# never came back and loss printed blank). A missing report is now an UNMEASURED FAIL that says so.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,7p' "$0"; usage_common; exit 0; }
: "${RATE:=3}" "${DUR:=$(q 900 240)}" "${MTUS:=default 940}"
suite_init; require_target; fail=0
deadman_cover "$DUR"
for mtu in $MTUS; do
  connect "$DEST" 15 || { verdict T24-sustained-upload FAIL "connect failed"; exit 1; }
  [ "$mtu" != default ] && in_client "ip link set dev $WG_IFACE mtu $mtu"
  eff=$(in_client "cat /sys/class/net/$WG_IFACE/mtu"); u0=$(telemetry_metric 'hopr_packet_rejected_count{reason="undecodable"}' || true)
  in_client "python3 /suite/probes/streamprobe.py --mode ul --host $TARGET_IP --port 8902 --rate-mbit $RATE --duration $DUR --size 1200 --iface $WG_IFACE --out ${SUITE_RUN_IN_CLIENT}/t24-$mtu.json" >/dev/null 2>&1 || true
  u1=$(telemetry_metric 'hopr_packet_rejected_count{reason="undecodable"}' || true); e=$(log_errors "$LOG_SINCE"); save_client_log "t24-$mtu" "$LOG_SINCE"; disconnect
  j=$(cat "$SUITE_RUN/t24-$mtu.json" 2>/dev/null || echo '{"loss_pct":100}'); loss=$(json_get "$j" loss_pct); rec=$(json_get "$e" reconnects)
  stalls=$(json_get "$j" stalls_gt_5s); sent=$(json_get "$j" sent); report=$(json_get "$j" server_report)
  # the probe's own view of the interface: an IFDOWN with no reconnect is the tunnel being torn down under the sender
  ifdown=$(python3 -c 'import sys,json; d=json.loads(sys.argv[1]); s=d.get("start") or 0
ev=[round(t-s) for t,e in d.get("events",[]) if e=="IFDOWN"]; print(ev[0] if ev else "")' "$j")
  emit_row T24-sustained-upload mtu="$mtu" effective_mtu="$eff" "result=$j" "errors=$e" undecodable_before="${u0:-null}" undecodable_after="${u1:-null}" deadman="$DEADMAN"
  du=$(python3 -c "print(int('${u1:-0}' or 0)-int('${u0:-0}' or 0))")
  base="mtu $eff, $RATE Mbit/s × ${DUR}s (sent ${sent:-0} pkts): reconnects ${rec:-?} (tunnel-ping timeouts $(json_get "$e" ping_timeouts)), undecodable +$du"
  if [ -z "$loss" ] || [ "$loss" = "None" ] || [ -z "$report" ] || [ "$report" = "null" ]; then
    verdict T24-sustained-upload FAIL "UNMEASURED — $base; the server's end-of-stream report never arrived${ifdown:+ (tunnel interface went down at +${ifdown}s of the arm)}, so loss is unknown"
    fail=1; continue
  fi
  msg="$base, loss ${loss}%, stalls>5s ${stalls:-0}${ifdown:+, interface down at +${ifdown}s}"
  python3 -c "import sys; sys.exit(0 if ${loss} < 5 and ${rec:-1} == 0 and $du < 50 else 1)" && verdict T24-sustained-upload PASS "$msg" || { verdict T24-sustained-upload FAIL "$msg"; fail=1; }
done
exit $fail
