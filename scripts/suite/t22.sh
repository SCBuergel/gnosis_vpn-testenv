#!/usr/bin/env bash
# T22-concurrent-clients — Concurrent client ladder (gate): N client containers download through the same exit at the same time,
# over the rungs in LADDER (default "1 2 4", clamped to the clients actually running). Reports per-client and
# aggregate throughput, completion, and fairness (slowest client over the rung mean).
# Pass: (a) every client connects and completes its transfer at every rung, and (b) aggregate throughput never
# COLLAPSES as clients are added: no higher rung may fall more than TOL_PCT below any lower rung's aggregate.
# Aggregate that RISES with concurrency is the healthy shape on this stack — one client cannot saturate the exit
# (rerun1 2026-09-20: n=1 10.6, n=2 13.7, n=4 16.1 Mbit/s, everyone complete, fairness 0.85 at n=4) — and the
# earlier rule, "every rung within TOL_PCT of the best rung", failed exactly that ladder as "drop 34 % at n=1".
# TOL_PCT is 25: T03-repeatability-baseline measures +-12-18 % on this host, so 25 sits outside the host's own
# noise and the gate catches a concurrency collapse without firing on jitter.
# Per-client throughput is EXPECTED to fall as clients are added — they share one exit and one host — so nothing
# is asserted per client beyond completion. Fairness (slowest client over the rung mean) is RECORDED, not gated:
# the starvation signature seen on the 2026-09-09/10 fleet ladders was zero-byte transfers, which (a) catches, and
# a fairness bound taken from one ladder would be a number we cannot justify. The top rung's aggregate is
# delta-scored against the previous run of this stack, which is where a gradual concurrency regression shows.
# Every client is WARMED before the measured transfer. Each rung connects its clients fresh, so without a warm-up
# every measured transfer is a cold start and the test measures the SURB ramp (T07-cold-start) instead of concurrency --
# observed 2026-09-17, when a single client moved 0 bytes at n=1 while four together moved 8.2 Mbit/s. Set
# WARMUP=0 to measure cold concurrency deliberately.
# Setup: CLIENT_COUNT=N with a cluster created with EXTRA_IDENTITIES=N, then `just clients-start`.
# Each client's row carries its warm-up result, whether it was Connected right before the measured transfer, its
# reconnects/tunnel-ping timeouts and any Disconnect command it received; a FAIL names all of it per client.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,20p' "$0"; usage_common; exit 0; }
: "${LADDER:=1 2 4}" "${TOL_PCT:=25}" "${WARMUP:=1}" "${WARMUP_BYTES:=500000}"
suite_init; require_target

avail=$(clients_running)
if [ "${avail:-0}" -lt 2 ]; then
  verdict T22-concurrent-clients SKIP "only ${avail:-0} client container(s) running; need at least 2 (CLIENT_COUNT=N + just clients-start)"
  exit 0
fi

rungs=""
for n in $LADDER; do [ "$n" -le "$avail" ] && rungs="$rungs $n"; done
[ -z "${rungs// /}" ] && { verdict T22-concurrent-clients SKIP "no ladder rung fits ${avail} running client(s)"; exit 0; }

# Every client is disconnected on the way out, whatever happens: each connect happens in a subshell whose EXIT
# trap disarms its own deadman, so an abandoned rung would otherwise leave clients connected with no deadman.
cleanup_clients() {
  local i
  for i in $(seq 1 "$avail"); do
    docker exec "$(client_name "$i")" gnosis_vpn-ctl disconnect >/dev/null 2>&1 || true
  done
}
trap 'cleanup_clients; disarm_deadman' EXIT

declare -A AGG
best=0; fail=0

# Every client's rung is diagnosed, not just measured: the warm-up result, whether the client was still Connected
# right before the measured transfer, its reconnects and tunnel-ping timeouts, and any Disconnect command the
# client received during the rung. fullrun5 read "1/1 did not complete, 0.0 Mbit/s" at n=1 and the only trace of
# why was one Disconnect line buried in 42 000 lines of path-planner debug: lib.sh's deadman disarm had torn the
# tunnel down 30 s after connect. A rung whose client was not connected at measurement time now says so.
run_rung() { # run_rung N
  local n=$1 i agg=0 incomplete=0 mbits=() j c since; since=$(utc_now)
  declare -A WARM CONN ERRS DCMD
  for i in $(seq 1 "$n"); do
    if ! CLIENT="$(client_name "$i")" bash -c "source '${SUITE_LIB_DIR}/lib.sh'; connect '${DEST}' 5" >/dev/null 2>&1; then
      verdict T22-concurrent-clients FAIL "n=${n}: client ${i} failed to connect"
      cleanup_clients; fail=1; return 1
    fi
  done
  # warm every client past the ramp before measuring, in parallel so the rung is not serialised
  if [ "$WARMUP" = 1 ]; then
    for i in $(seq 1 "$n"); do
      ( CLIENT="$(client_name "$i")" bash -c "source '${SUITE_LIB_DIR}/lib.sh'; curl_down '${TARGET_IP}' '${WARMUP_BYTES}' '${CAP}'" \
          > "${SUITE_RUN}/t22-n${n}-c${i}-warm.json" 2>/dev/null ) &
    done
    wait
    sleep 3
  fi
  for i in $(seq 1 "$n"); do
    WARM[$i]=$(cat "${SUITE_RUN}/t22-n${n}-c${i}-warm.json" 2>/dev/null || echo '{}')
    if docker exec "$(client_name "$i")" gnosis_vpn-ctl status 2>/dev/null | grep -q '^Connected to'; then CONN[$i]=true; else CONN[$i]=false; fi
  done
  for i in $(seq 1 "$n"); do
    ( CLIENT="$(client_name "$i")" bash -c "source '${SUITE_LIB_DIR}/lib.sh'; curl_down '${TARGET_IP}' '${BYTES}' '${CAP}'" \
        > "${SUITE_RUN}/t22-n${n}-c${i}.json" 2>/dev/null ) &
  done
  wait
  for i in $(seq 1 "$n"); do
    c=$(client_name "$i")
    ERRS[$i]=$(log_errors "$since" "$c")
    DCMD[$i]=$(docker logs --since "$since" "$c" 2>&1 | grep -a -c 'received socket command.*command=Disconnect' || true)
    save_client_log "t22-n${n}-c${i}" "$since" "$c"
  done
  cleanup_clients
  local why=""
  for i in $(seq 1 "$n"); do
    j=$(cat "${SUITE_RUN}/t22-n${n}-c${i}.json" 2>/dev/null || echo '{"mbit":0,"complete":false}')
    mbits+=("$(json_get "$j" mbit)")
    emit_row T22-concurrent-clients n="$n" client="$i" "result=$j" "warmup=${WARM[$i]}" connected_before="${CONN[$i]}" "errors=${ERRS[$i]}" disconnect_cmds="${DCMD[$i]:-0}"
    if [ "$(json_get "$j" complete)" != "True" ] && [ "$(json_get "$j" complete)" != "true" ]; then
      incomplete=$((incomplete+1))
      why="${why:+$why; }client ${i}: $(json_get "$j" bytes) bytes (http $(json_get "$j" code)), warm-up $(json_get "${WARM[$i]}" bytes) bytes, $([ "${CONN[$i]}" = true ] && echo "Connected" || echo "NOT CONNECTED") before the transfer, reconnects $(json_get "${ERRS[$i]}" reconnects) (tunnel-ping timeouts $(json_get "${ERRS[$i]}" ping_timeouts)), Disconnect commands ${DCMD[$i]:-0}"
    fi
  done
  agg=$(python3 -c "print(round(sum(float(x or 0) for x in '${mbits[*]}'.split()),3))")
  local fair; fair=$(python3 -c "
v=[float(x or 0) for x in '${mbits[*]}'.split()]
print(round(min(v)/(sum(v)/len(v)),2) if v and sum(v)>0 else 0)")
  AGG[$n]=$agg
  emit_row T22-concurrent-clients n="$n" kind=rung aggregate_mbit="$agg" fairness="$fair" incomplete="$incomplete"
  if [ "$incomplete" -gt 0 ]; then
    verdict T22-concurrent-clients FAIL "n=${n}: ${incomplete}/${n} client(s) did not complete the transfer within ${CAP}s (aggregate ${agg} Mbit/s, warmup=${WARMUP}) — ${why}"
    fail=1
  else
    record T22-concurrent-clients "n=${n}: aggregate ${agg} Mbit/s over [${mbits[*]}], slowest/mean ${fair}, all ${n} complete, reconnects $(python3 -c "import sys; print(sum(int(float(x or 0)) for x in sys.argv[1:]))" $(for i in $(seq 1 "$n"); do json_get "${ERRS[$i]}" reconnects; done))"
  fi
  python3 -c "import sys; sys.exit(0 if ${agg} > ${best} else 1)" && best=$agg
  return 0
}

for n in $rungs; do run_rung "$n" || break; done

# (b) aggregate must not collapse as clients are added: every higher rung against every lower rung, one-sided
if [ "$fail" = 0 ]; then
  worst_drop=0; worst_pair=""
  for n in $rungs; do
    for m in $rungs; do
      [ "$m" -lt "$n" ] || continue
      d=$(python3 -c "a=${AGG[$m]}; b=${AGG[$n]}; print(round((a-b)/a*100,1) if a>0 else 0)")
      python3 -c "import sys; sys.exit(0 if $d > $worst_drop else 1)" && { worst_drop=$d; worst_pair="n=${m} -> n=${n}"; }
    done
  done
  summary=$(for n in $rungs; do printf 'n=%s %s  ' "$n" "${AGG[$n]}"; done)
  if python3 -c "import sys; sys.exit(0 if ${worst_drop} <= ${TOL_PCT} else 1)"; then
    verdict T22-concurrent-clients PASS "aggregate holds or grows with concurrency up to ${rungs##* } clients: ${summary}Mbit/s (worst step ${worst_pair:-none} ${worst_drop}%, tolerance ${TOL_PCT}%)"
  else
    verdict T22-concurrent-clients FAIL "aggregate collapses with concurrency: ${summary}Mbit/s (${worst_pair} drops ${worst_drop}%, tolerance ${TOL_PCT}%)"
  fi
  score_delta T22-concurrent-clients concurrent_aggregate_mbit "${AGG[${rungs##* }]}" higher_better
fi
exit $SUITE_FAILED
