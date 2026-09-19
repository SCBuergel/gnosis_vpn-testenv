#!/usr/bin/env bash
# T11-capability-matrix — WireGuard session capability matrix (gate). Cells over [connection.wg] capabilities: the default
# segmentation+no_delay, segmentation only, and the default plus no_rate_control.
# The exit's datagram mode keys off the client's no_delay flag and its egress shaper exists exactly when
# no_rate_control is absent, so this catches a capability-dependency regression on either side.
# Observability: the shaper is detected from the exit's per-session SURB-balancer metrics — the exit publishes
# hopr_surb_balancer_{control_output,current_buffer_estimate,current_buffer_target,surbs_rate,
# target_error_estimate} labelled with the session_id exactly while it is shaping that session. Verified on
# hoprd 4.1.2: five such series appear when connected and none when idle, while the
# "spawning exit SURB balancer" debug line is not emitted at the default node log level at all — which is why
# this gate reads metrics rather than logs.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,9p' "$0"; usage_common; exit 0; }
# Env: CALL_S (call-probe seconds; 60, fast 30) POLL_N (shaper-metric polls at 2 s each; 10).
: "${CALL_S:=$(q 60 30)}" "${POLL_N:=10}"
suite_init; require_target
cfg="$CONFIG_DIR/client.toml"; cp "$cfg" "$SUITE_RUN/client.toml.t11.orig"
restore() { cp "$SUITE_RUN/client.toml.t11.orig" "$cfg"; client_restart || true; }
trap 'restore; disarm_deadman' EXIT
# shaper_series — number of live per-session shaper series on the exit right now
shaper_series() {
  node_metrics 0 | grep -cE '^hopr_surb_balancer_[a-z_]+\{[^}]*session_id=' 2>/dev/null || true
}
fail=0
run_cell() { # name caps expect_shaper
  local name=$1 caps=$2 expect=$3 wgt
  wgt=$(grep -A2 '^\[connection.wg\]' "$SUITE_RUN/client.toml.t11.orig" | grep target || echo 'target = "127.0.0.1:51821"')
  python3 "$SUITE_LIB_DIR/toml-edit.py" "$cfg" set-section '[connection.wg]' "capabilities = $caps" "$wgt"
  client_restart || { verdict T11-capability-matrix FAIL "$name: client restart failed"; fail=1; return; }
  local before; before=$(shaper_series)
  connect "$DEST" 0 || { verdict T11-capability-matrix FAIL "$name: connect failed"; fail=1; return; }
  local during=0 i=0
  while [ "$i" -lt "$POLL_N" ]; do local n; n=$(shaper_series); [ "$n" -gt "$during" ] && during=$n; sleep 2; i=$((i+1)); done
  local r; r=$(curl_down "$TARGET_IP" "$BYTES" "$CAP")
  in_client "python3 /suite/probes/relprobe.py --host $TARGET_IP --port 8901 --rate-mbit 1.5 --duration $CALL_S --size 1200 --iface $WG_IFACE --out ${SUITE_RUN_IN_CLIENT}/t11-$name" >/dev/null 2>&1 || true
  local e; e=$(log_errors "$LOG_SINCE"); disconnect
  local call; call=$(cat "$SUITE_RUN/t11-$name.json" 2>/dev/null || echo '{}')
  local shaped=0; [ "$during" -gt "$before" ] && shaped=1
  emit_row T11-capability-matrix cell="$name" caps="$caps" "download=$r" "call=$call" "errors=$e" shaper_series_before="$before" shaper_series_peak="$during" shaped="$shaped" expect_shaper="$expect"
  local msg="$name $caps: decap $(json_get "$e" decap_error), call loss $(json_get "$call" loss_pct)%, exit shaper series $before->$during (shaped=$shaped, expected $expect), down $(json_get "$r" mbit) Mbit/s"
  if [ "$(json_get "$e" decap_error)" = 0 ] && [ "$shaped" = "$expect" ]; then verdict T11-capability-matrix PASS "$msg"
  else verdict T11-capability-matrix FAIL "$msg"; fail=1; fi
}
run_cell default          '["segmentation", "no_delay"]' 1
run_cell segmentation-only '["segmentation"]' 1
run_cell no-rate-control  '["segmentation", "no_delay", "no_rate_control"]' 0
restore; trap 'disarm_deadman' EXIT
exit $fail
