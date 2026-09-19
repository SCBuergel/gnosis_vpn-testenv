#!/usr/bin/env bash
# T12-balancer-sweep — SURB balancer profile sweep (gate; absorbs the former config-default-vs-tuned test as its masking cell): cells over [connection.surb_balancing.main] max_surb_upstream {12,16,48,96 Mb/s}
# and the ping tier {1 MB/512 Kb/s, 10 MB/16 Mb/s}; per cell connect time, a cold first download, a warm T04-fixed-throughput rep, and
# the exit node's logged session target. Reverse order on a second pass. Pass: no cell collapses (download > 0.3× best).
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,5p' "$0"; usage_common; exit 0; }
: "${UPSTREAMS:=$(q "12 16 48 96" "16 96")}" "${PASSES:=$(q 2 1)}"
suite_init; require_target
cfg="$CONFIG_DIR/client.toml"; cp "$cfg" "$SUITE_RUN/client.toml.t12.orig"
restore() { cp "$SUITE_RUN/client.toml.t12.orig" "$cfg"; client_restart || true; }
trap 'restore; disarm_deadman' EXIT
best=0; worst=999999; cells=()
for u in $UPSTREAMS; do cells+=("main:$u"); done; cells+=("ping:10MB")
run_cell() {
  local c=$1
  cp "$SUITE_RUN/client.toml.t12.orig" "$cfg"
  case "$c" in
    main:*) python3 "$SUITE_LIB_DIR/toml-edit.py" "$cfg" set-section '[connection.surb_balancing.main]' 'enabled = true' 'buffer = "10 MB"' "max_surb_upstream = \"${c#main:} Mb/s\"";;
    ping:10MB) python3 "$SUITE_LIB_DIR/toml-edit.py" "$cfg" set-section '[connection.surb_balancing.ping]' 'enabled = true' 'buffer = "10 MB"' 'max_surb_upstream = "16 Mb/s"';;
  esac
  client_restart || { verdict T12-balancer-sweep FAIL "$c: client restart failed"; return; }
  connect "$DEST" 0 || { verdict T12-balancer-sweep FAIL "$c: connect failed"; return; }
  local cms=$CONNECT_MS; local cold; cold=$(curl_down "$TARGET_IP" "$BYTES" "$CAP"); sleep 20; local warm; warm=$(curl_down "$TARGET_IP" "$BYTES" "$CAP")
  local e; e=$(log_errors "$LOG_SINCE"); local tgt; tgt=$(grep "spawning exit SURB balancer" "$(node_log_file 0)" 2>/dev/null | tail -1 | grep -oE 'target_surb_buffer_size: [0-9]+' || true); disconnect
  emit_row T12-balancer-sweep cell="$c" connect_ms="$cms" "cold=$cold" "warm=$warm" "errors=$e" exit_target="$tgt"
  local w; w=$(json_get "$warm" mbit); best=$(python3 -c "print(max($best,$w))"); worst=$(python3 -c "print(min($worst,$w))")
  suite_log "$c: connect ${cms} ms, cold $(json_get "$cold" mbit), warm $w Mbit/s, discards $(json_get "$e" frame_discarded), exit $tgt"
}
for p in $(seq 1 "$PASSES"); do
  if [ $((p % 2)) = 1 ]; then for c in "${cells[@]}"; do run_cell "$c"; done; else for ((i=${#cells[@]}-1;i>=0;i--)); do run_cell "${cells[$i]}"; done; fi
done
restore; trap 'disarm_deadman' EXIT
python3 -c "import sys; sys.exit(0 if $best>0 and $worst >= 0.3*$best else 1)" && verdict T12-balancer-sweep PASS "warm download across cells: worst $worst, best $best Mbit/s (no collapse)" || verdict T12-balancer-sweep FAIL "a cell collapsed: worst $worst vs best $best Mbit/s"
# masking cell (former config-default-vs-tuned test): a raised ping tier must not be what makes a cold start work. If the default ping
# tier fails the cold start and the raised one passes, the tuning is masking a default-config defect.
cp "$SUITE_RUN/client.toml.t12.orig" "$cfg"; client_restart || true
connect "$DEST" 0 && { d_def=$(curl_down "$TARGET_IP" "$BYTES" "$CAP"); disconnect; } || d_def='{"mbit":0,"complete":false}'
python3 "$SUITE_LIB_DIR/toml-edit.py" "$cfg" set-section '[connection.surb_balancing.ping]' 'enabled = true' 'buffer = "10 MB"' 'max_surb_upstream = "16 Mb/s"'
client_restart || true
connect "$DEST" 0 && { d_tun=$(curl_down "$TARGET_IP" "$BYTES" "$CAP"); disconnect; } || d_tun='{"mbit":0,"complete":false}'
emit_row T12-balancer-sweep cell=masking "default_ping_tier=$d_def" "raised_ping_tier=$d_tun"
mdef=$(json_get "$d_def" mbit); mtun=$(json_get "$d_tun" mbit)
if [ "$(json_get "$d_def" complete)" = "True" ]; then
  verdict T12-balancer-sweep PASS "masking: cold start completes on the DEFAULT ping tier ($mdef Mbit/s; raised tier $mtun)"
else
  verdict T12-balancer-sweep FAIL "masking: cold start fails on defaults ($mdef Mbit/s) but the raised ping tier gives $mtun — tuning is hiding a default-config defect"
fi
exit $SUITE_FAILED
