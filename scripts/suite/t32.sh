#!/usr/bin/env bash
# T32-congestion-control — Client-host TCP congestion control A/B: cubic vs bbr (+fq) set inside the client's network namespace
# (docker --sysctl, restart per arm), ABBA order over PAIRS pairs; upload is the treated direction, download the
# null-direction control. Needs tcp_bbr loaded on the host. Reports paired ratios and sign counts (R2–R4).
source "$(dirname "$0")/lib.sh"
suite_kind runbook
[ "${1:-}" = "--help" ] && { sed -n '2,5p' "$0"; usage_common; exit 0; }
: "${PAIRS:=$(q 6 3)}"
suite_init; require_target
grep -qw bbr /proc/sys/net/ipv4/tcp_available_congestion_control || { verdict T32-congestion-control SKIP "tcp_bbr not available on the host (modprobe tcp_bbr)"; exit 0; }
restart_cc() { ( cd "$TESTENV_DIR" && just client-stop >/dev/null 2>&1; CLIENT_SYSCTL="net.ipv4.tcp_congestion_control=$1" just client-start >/dev/null ); sleep 3; wait_worker 180; }
measure() { # cc -> "down up"
  connect "$DEST" 15 || { echo "0 0"; return; }
  local cc; cc=$(in_client 'cat /proc/sys/net/ipv4/tcp_congestion_control')
  local d u; d=$(curl_down "$TARGET_IP" "$BYTES" "$CAP"); u=$(curl_up "$TARGET_IP" "$BYTES" "$CAP"); disconnect
  emit_row T32-congestion-control cc="$cc" "down=$d" "up=$u"; echo "$(json_get "$d" mbit) $(json_get "$u" mbit)"
}
A=(); B=()
for p in $(seq 1 "$PAIRS"); do
  if [ $((p % 2)) = 1 ]; then order="cubic bbr"; else order="bbr cubic"; fi
  for cc in $order; do restart_cc "$cc"; v=$(measure "$cc"); if [ "$cc" = cubic ]; then A+=("$v"); else B+=("$v"); fi; done
done
( cd "$TESTENV_DIR" && just client-stop >/dev/null 2>&1; just client-start >/dev/null ); wait_worker 180 || true
res=$(python3 - "${A[*]}" "${B[*]}" <<'PY'
import sys,statistics as st,json
A=[tuple(map(float,x.split())) for x in sys.argv[1].split("  ") if x] if "  " in sys.argv[1] else None
def parse(s):
    v=s.split(); return [(float(v[i]),float(v[i+1])) for i in range(0,len(v)-1,2)]
A=parse(sys.argv[1]); B=parse(sys.argv[2]); n=min(len(A),len(B))
def cmp(idx):
    ra=[b[idx]/a[idx] for a,b in zip(A,B) if a[idx]>0]; wins=sum(1 for r in ra if r>1)
    return {"median_ratio_bbr_over_cubic":round(st.median(ra),3) if ra else None,"bbr_faster":wins,"n":len(ra)}
print(json.dumps({"upload":cmp(1),"download_control":cmp(0),"pairs":n}))
PY
)
emit_row T32-congestion-control kind=summary "result=$res"
verdict T32-congestion-control PASS "upload bbr/cubic $(json_get "$res" upload.median_ratio_bbr_over_cubic) ($(json_get "$res" upload.bbr_faster)/$(json_get "$res" upload.n) bbr faster); download control $(json_get "$res" download_control.median_ratio_bbr_over_cubic) ($(json_get "$res" download_control.bbr_faster)/$(json_get "$res" download_control.n))"
exit 0
