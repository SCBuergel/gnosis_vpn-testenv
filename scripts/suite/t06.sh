#!/usr/bin/env bash
# T06-realtime-udp — behaviour under constant bitrate (gate). Three arms, each on its own session: one LONG bidirectional
# echo call (relprobe → :8901) at ECHO_RATE Mbit/s for ECHO_DUR s, which is the gate and is long enough to show the
# reconnect cycle; then upload-only and download-only streams (streamprobe → :8902) at STREAM_RATE for STREAM_DUR s,
# which discriminate the direction once the tunnel survives. This replaced six 300 s arms (three directions x three
# rates) on 2026-09-19: across two full runs every arm reconnected at ~50 s regardless of rate or direction, so the
# rate axis discriminated nothing. That reconnect turned out to be the client's periodic liveness ping targeting an
# address the server did not hold (T01-topology-preconditions now checks it), not the load; the three-arm shape
# stays because it measures more in less time.
#
# Every arm returns a binary verdict: NO reconnect during the arm, loss below LOSS_MAX %, no stall over
# STALL_MAX s, and the probe must actually have sent what it set out to send.
#
# A RECONNECT IS A HARD FAILURE ON ITS OWN. The client's watchdog tearing the tunnel down mid-flow fails the arm
# whatever the loss figure says, and the verdict names the count, the tunnel-ping timeouts that preceded it, the
# probe's rebinds and the outage it measured. Three ping timeouts per reconnect means the liveness ping itself is
# failing, which is a target/config problem before it is a load problem. The probes re-bind their socket when the interface is
# recreated (a socket bound to the removed interface goes blind silently, which on 2026-09-19 made every arm
# read "loss = (arm length - 50 s) / arm length"), so loss now means loss on the live path and the outage is
# reported next to it.
#
# THE SAMPLE GUARD IS THE POINT. A probe whose session is broken sends a handful of packets and still reports a
# confident loss percentage from that handful. On the 2026-09-17 old-version run the dl-3Mbit arm sent 255 of an
# expected 4688 packets and reported "60.78 % loss", which reads like a measurement and is not one. An arm that
# sent less than SAMPLE_MIN_PCT of its expected packet count is UNMEASURED and fails as such, naming the counts.
#
# EACH ARM GETS ITS OWN SESSION. The arms used to share one connect, which made them order-dependent: on that
# same run echo-1.5Mbit lost 82.7 % and echo-3Mbit, immediately after it on the same session, lost 0.41 %. Arms
# that cannot be compared across runs are not measurements. PER_ARM_SESSION=0 restores the old shared session.
#
# There is deliberately NO XFAIL here. An earlier revision asserted "the bidirectional arm above 1.5 Mbit/s must
# lose more than 50 %" as a known defect, calibrated on a number (65.6 %) later found to be contaminated by the
# SURB ramp and by arm ordering; the same arm then measured 31 % and 0.41 % on other stacks. A bound we cannot
# justify is worse than a plain gate, because XFAIL silences the arm. Re-introduce one only from post-ramp,
# per-arm-session baselines on a stack known to be good.
#
# Env: ECHO_RATE=1.5 ECHO_DUR (300 s; fast 120) STREAM_RATE=3 STREAM_DUR (120 s; fast 90) LOSS_MAX (5) STALL_MAX (5) SIZE (1200)
#      SAMPLE_MIN_PCT (80) PER_ARM_SESSION (1) AFTER_BULK (1: a fourth arm, the download stream after bulk transfers)
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,23p' "$0"; usage_common; exit 0; }
: "${ECHO_RATE:=1.5}" "${ECHO_DUR:=$(q 300 120)}" "${STREAM_RATE:=3}" "${STREAM_DUR:=$(q 120 90)}" "${LOSS_MAX:=5}" "${STALL_MAX:=5}" "${SIZE:=1200}"
: "${SAMPLE_MIN_PCT:=80}" "${PER_ARM_SESSION:=1}"
suite_init; require_target

connected=0
arm_connect() {
  [ "$PER_ARM_SESSION" = 0 ] && [ "$connected" = 1 ] && return 0
  connect "$DEST" 0 || return 1
  connected=1
}
arm_disconnect() { [ "$PER_ARM_SESSION" = 0 ] || { disconnect; connected=0; }; }

# expected_pkts RATE DUR SIZE — what a probe at this rate should have sent
expected_pkts() { python3 -c "print(int(float('$1')*1e6/8/float('$3')*float('$2')))"; }

check() { # check label rate dur json errs
  local l=$1 r=$2 dur=$3 j=$4 e=${5:-'{}'} loss stall worst p99 sent exp pct rec rebinds outage
  loss=$(json_get "$j" loss_pct); stall=$(json_get "$j" stalls_gt_5s); sent=$(json_get "$j" sent)
  worst=$(python3 -c 'import sys,json; w=json.loads(sys.argv[1]).get("worst_stalls") or []; print(w[0][1] if w else 0)' "$j")
  p99=$(json_get "$j" delay_over_min_ms.p99 2>/dev/null || json_get "$j" rtt_ms.p99)
  exp=$(expected_pkts "$r" "$dur" "$SIZE")
  rec=$(json_get "$e" reconnects); rebinds=$(json_get "$j" rebinds); outage=$(json_get "$j" outage_total_s)
  case "$stall" in ''|None) stall=0;; esac
  case "$sent" in ''|None) sent=0;; esac
  case "$rec" in ''|None) rec=0;; esac
  case "$rebinds" in ''|None) rebinds=0;; esac
  case "$outage" in ''|None) outage="n/a (upload: see the server report's stalls)";; esac
  pct=$(python3 -c "print(round(${sent}*100.0/max(${exp},1),1))")
  emit_row T06-realtime-udp label="$l" rate_mbit="$r" sent_pkts="$sent" expected_pkts="$exp" sent_pct="$pct" "result=$j" "errors=$e"

  # 1. did the tunnel survive the arm? A reconnect is the defect itself; it fails the arm before any loss figure is read.
  if [ "$rec" -gt 0 ] 2>/dev/null; then
    verdict T06-realtime-udp FAIL "$l: RECONNECT during the arm — client reconnects ${rec} after $(json_get "$e" ping_timeouts) tunnel-ping timeouts, probe rebinds ${rebinds}, outage ${outage}s; loss on the live path ${loss:-none}%, sample ${pct}% of expected"
    return
  fi
  # 2. did the probe run at all?
  if python3 -c "import sys; sys.exit(0 if ${pct} < ${SAMPLE_MIN_PCT} else 1)"; then
    verdict T06-realtime-udp FAIL "$l: UNMEASURED — probe sent ${sent} of ${exp} expected packets (${pct}%, floor ${SAMPLE_MIN_PCT}%); any loss figure from this arm is meaningless (reported ${loss:-none}%)"
    return
  fi
  # 3. did it report anything?
  if [ -z "$loss" ] || [ "$loss" = "None" ]; then
    verdict T06-realtime-udp FAIL "$l: no loss figure — the probe's report never arrived (sent ${sent} packets in $(json_get "$j" duration_s) s)"
    return
  fi
  # 4. the actual assertions
  local ok=1 why=""
  python3 -c "import sys; sys.exit(0 if ${loss} < ${LOSS_MAX} else 1)" || { ok=0; why="loss ${loss}% >= ${LOSS_MAX}%"; }
  if [ "${stall}" -gt 0 ] 2>/dev/null; then ok=0; why="${why:+$why; }${stall} stall(s) over ${STALL_MAX}s, worst ${worst}s"; fi
  if [ "$ok" = 1 ]; then
    verdict T06-realtime-udp PASS "$l: loss ${loss}%, worst stall ${worst}s, p99 ${p99} ms, sample ${pct}% of expected, no reconnect"
  else
    verdict T06-realtime-udp FAIL "$l: ${why} (p99 ${p99} ms, sample ${pct}% of expected, no reconnect)"
  fi
  score_delta T06-realtime-udp "delivered_pct_${l}" "$(python3 -c "print(round(100-${loss},2))")" higher_better
}

node_sampler_start t06 1
# arm 1 — the gate: one long bidirectional call at the realistic rate, long enough to show the reconnect CYCLE
# (a tunnel that dies every ~80 s dies three or four times in 300 s) and what the recovered session does between deaths.
r=$ECHO_RATE
arm_connect || verdict T06-realtime-udp FAIL "echo-${r}Mbit: connect failed"
if [ "$connected" = 1 ]; then
  in_client "python3 /suite/probes/relprobe.py --host $TARGET_IP --port 8901 --rate-mbit $r --duration $ECHO_DUR --size $SIZE --iface $WG_IFACE --out ${SUITE_RUN_IN_CLIENT}/t06-echo-$r" >/dev/null 2>&1 || true
  # the arm's own log slice: LOG_SINCE was set by this arm's connect (or by the shared session's)
  check "echo-${r}Mbit" "$r" "$ECHO_DUR" "$(cat "$SUITE_RUN/t06-echo-$r.json" 2>/dev/null || echo '{"loss_pct":100,"sent":0}')" "$(log_errors "$LOG_SINCE")"
  save_client_log "t06-echo-$r" "$LOG_SINCE"
  arm_disconnect
fi
# arms 2 and 3 — the direction discriminators: uploads ride the forward path, downloads the SURB-metered return
# path, and they fail differently once the tunnel survives; one rate each, at the top of the realistic range.
r=$STREAM_RATE
for m in ul dl; do
  arm_connect || { verdict T06-realtime-udp FAIL "${m}-${r}Mbit: connect failed"; continue; }
  in_client "python3 /suite/probes/streamprobe.py --mode $m --host $TARGET_IP --port 8902 --rate-mbit $r --duration $STREAM_DUR --size $SIZE --iface $WG_IFACE --out ${SUITE_RUN_IN_CLIENT}/t06-$m-$r.json" >/dev/null 2>&1 || true
  check "${m}-${r}Mbit" "$r" "$STREAM_DUR" "$(cat "$SUITE_RUN/t06-$m-$r.json" 2>/dev/null || echo '{"loss_pct":100,"sent":0}')" "$(log_errors "$LOG_SINCE")"
  save_client_log "t06-$m-$r" "$LOG_SINCE"
  arm_disconnect
done
# arm 4 — the download stream again, but on a session that has just carried bulk transfers. T09-impairment-ladder
# used to run its stream this way and every unimpaired cell failed on it (hoprd 4.1.3, client 0.96.3, three runs on
# 2026-09-20: 9-54 reassembly failures, 16-71 % loss, one reconnect on some rungs) while the fresh-session arm above
# read 0.2 % loss. Whatever the bulk transfers leave on the session is the defect this arm isolates. AFTER_BULK=0 skips it.
if [ "${AFTER_BULK:=1}" = 1 ]; then
  if arm_connect; then
    transfer_series "t06-bulk" "$TARGET_IP" "$(q "$REPS" 1)" "$BYTES" "$CAP" T06-realtime-udp >/dev/null
    in_client "python3 /suite/probes/streamprobe.py --mode dl --host $TARGET_IP --port 8902 --rate-mbit $r --duration $STREAM_DUR --size $SIZE --iface $WG_IFACE --out ${SUITE_RUN_IN_CLIENT}/t06-dl-after-bulk-$r.json" >/dev/null 2>&1 || true
    check "dl-after-bulk-${r}Mbit" "$r" "$STREAM_DUR" "$(cat "$SUITE_RUN/t06-dl-after-bulk-$r.json" 2>/dev/null || echo '{"loss_pct":100,"sent":0}')" "$(log_errors "$LOG_SINCE")"
    save_client_log "t06-dl-after-bulk-$r" "$LOG_SINCE"
    arm_disconnect
  else
    verdict T06-realtime-udp FAIL "dl-after-bulk-${r}Mbit: connect failed"
  fi
fi
node_sampler_stop; errs=$(log_errors "$LOG_SINCE"); save_client_log t06 "$LOG_SINCE"
[ "$connected" = 1 ] && disconnect
emit_row T06-realtime-udp kind=summary "errors=$errs" echo_rate="$ECHO_RATE" echo_dur="$ECHO_DUR" stream_rate="$STREAM_RATE" stream_dur="$STREAM_DUR" per_arm_session="$PER_ARM_SESSION"
exit $SUITE_FAILED
