#!/usr/bin/env bash
# T22-concurrent-clients — Concurrent client ladder (gate): N client containers download through the same exit at the same time,
# over the rungs in LADDER (default "1 2 4", clamped to the clients actually running). Reports per-client and
# aggregate throughput, completion, and fairness (slowest client over the rung mean).
# Pass: (a) every client completes its transfer at every rung, and (b) aggregate throughput at every rung stays
# within TOL_PCT of the best rung.
# TOL_PCT is 25. T03-repeatability-baseline's measured repeatability band on this host is about +-18 %, so 25 % sits just outside the
# host's own noise: the gate catches a concurrency collapse without firing on jitter. Per-client throughput is
# EXPECTED to fall as clients are added — they share one exit and one host — so the assertion is on the
# aggregate, not per client. A rung that starves one client shows up in (a), because a starved client does not
# complete inside CAP.
# Every client is WARMED before the measured transfer. Each rung connects its clients fresh, so without a warm-up
# every measured transfer is a cold start and the test measures the SURB ramp (T07-cold-start) instead of concurrency --
# observed 2026-09-17, when a single client moved 0 bytes at n=1 while four together moved 8.2 Mbit/s. Set
# WARMUP=0 to measure cold concurrency deliberately.
# Setup: CLIENT_COUNT=N with a cluster created with EXTRA_IDENTITIES=N, then `just clients-start`.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,12p' "$0"; usage_common; exit 0; }
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

run_rung() { # run_rung N
  local n=$1 i agg=0 incomplete=0 mbits=() j since; since=$(utc_now)
  for i in $(seq 1 "$n"); do
    if ! CLIENT="$(client_name "$i")" bash -c "source '${SUITE_LIB_DIR}/lib.sh'; connect '${DEST}' 5" >/dev/null 2>&1; then
      verdict T22-concurrent-clients FAIL "n=${n}: client ${i} failed to connect"
      cleanup_clients; fail=1; return 1
    fi
  done
  # warm every client past the ramp before measuring, in parallel so the rung is not serialised
  if [ "$WARMUP" = 1 ]; then
    for i in $(seq 1 "$n"); do
      ( CLIENT="$(client_name "$i")" bash -c "source '${SUITE_LIB_DIR}/lib.sh'; curl_down '${TARGET_IP}' '${WARMUP_BYTES}' '${CAP}'" >/dev/null 2>&1 ) &
    done
    wait
    sleep 3
  fi
  for i in $(seq 1 "$n"); do
    ( CLIENT="$(client_name "$i")" bash -c "source '${SUITE_LIB_DIR}/lib.sh'; curl_down '${TARGET_IP}' '${BYTES}' '${CAP}'" \
        > "${SUITE_RUN}/t22-n${n}-c${i}.json" 2>/dev/null ) &
  done
  wait
  for i in $(seq 1 "$n"); do
    j=$(cat "${SUITE_RUN}/t22-n${n}-c${i}.json" 2>/dev/null || echo '{"mbit":0,"complete":false}')
    mbits+=("$(json_get "$j" mbit)")
    [ "$(json_get "$j" complete)" = "True" ] || [ "$(json_get "$j" complete)" = "true" ] || incomplete=$((incomplete+1))
    emit_row T22-concurrent-clients n="$n" client="$i" "result=$j"
  done
  for i in $(seq 1 "$n"); do docker logs --since "$since" "$(client_name "$i")" > "${SUITE_RUN}/logs/t22-n${n}-c${i}.log" 2>&1 || true; done
  cleanup_clients
  agg=$(python3 -c "print(round(sum(float(x or 0) for x in '${mbits[*]}'.split()),3))")
  local fair; fair=$(python3 -c "
v=[float(x or 0) for x in '${mbits[*]}'.split()]
print(round(min(v)/(sum(v)/len(v)),2) if v and sum(v)>0 else 0)")
  AGG[$n]=$agg
  emit_row T22-concurrent-clients n="$n" kind=rung aggregate_mbit="$agg" fairness="$fair" incomplete="$incomplete"
  if [ "$incomplete" -gt 0 ]; then
    verdict T22-concurrent-clients FAIL "n=${n}: ${incomplete}/${n} client(s) did not complete the transfer within ${CAP}s (aggregate ${agg} Mbit/s, warmup=${WARMUP})"
    fail=1
  else
    record T22-concurrent-clients "n=${n}: aggregate ${agg} Mbit/s over [${mbits[*]}], slowest/mean ${fair}, all ${n} complete"
  fi
  python3 -c "import sys; sys.exit(0 if ${agg} > ${best} else 1)" && best=$agg
  return 0
}

for n in $rungs; do run_rung "$n" || break; done

# (b) aggregate must hold across the ladder
if [ "$fail" = 0 ]; then
  worst_n=0; worst_drop=0
  for n in $rungs; do
    d=$(python3 -c "print(round((${best}-${AGG[$n]})/${best}*100,1) if ${best}>0 else 100)")
    python3 -c "import sys; sys.exit(0 if $d > $worst_drop else 1)" && { worst_drop=$d; worst_n=$n; }
  done
  summary=$(for n in $rungs; do printf 'n=%s %s  ' "$n" "${AGG[$n]}"; done)
  if python3 -c "import sys; sys.exit(0 if ${worst_drop} <= ${TOL_PCT} else 1)"; then
    verdict T22-concurrent-clients PASS "aggregate holds to ${avail} concurrent clients: ${summary}Mbit/s (worst drop ${worst_drop}% at n=${worst_n}, tolerance ${TOL_PCT}%)"
  else
    verdict T22-concurrent-clients FAIL "aggregate collapses with concurrency: ${summary}Mbit/s (drop ${worst_drop}% at n=${worst_n} vs best ${best}, tolerance ${TOL_PCT}%)"
  fi
  score_delta T22-concurrent-clients concurrent_aggregate_mbit "${AGG[${rungs##* }]}" higher_better
fi
exit $SUITE_FAILED
