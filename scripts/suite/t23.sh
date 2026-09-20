#!/usr/bin/env bash
# T23-sustained-soak — Sustained soak (gate; absorbs the former reconnect-cycle endurance test's one live covariate, worker RSS): a 1.5 Mbit/s bidirectional call plus a 25 MB transfer pair every INTERVAL s for DUR s,
# sampling error counters, reconnects, worker RSS and client log growth. Env: DUR (3600; fast 600).
# Pass: no reconnect, no unbounded RSS growth (last < 2× first), log rate < LOG_MB_MIN_MAX MB/min (200), call loss < CALL_LOSS_MAX % (5).
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,5p' "$0"; usage_common; exit 0; }
# LOG_MB_MIN_MAX is 200: the client runs at hopr_transport::path=debug for T08-relay-attribution and writes ~70 MB/min
# connected (measured 67.6 MB/min on 2026-09-19 once reconnects stopped recreating the log file and hiding the rate).
# The gate exists for logging hot loops (1.6 GB/min was the incident), and 200 is 3x the normal rate.
: "${DUR:=$(q 3600 600)}" "${INTERVAL:=300}" "${LOG_MB_MIN_MAX:=200}" "${CALL_LOSS_MAX:=5}"
# CALL_LOSS_MAX is T06-realtime-udp's LOSS_MAX for the same probe at the same rate: the 1.5 Mbit/s call is the soak's
# primary load, and a soak that delivers 24 % of it must not pass (fullrun5 did, on the deadman bug, because loss
# was reported and not asserted). A missing call report counts as 100 % loss.
suite_init; require_target
# the soak must outlive the deadman (900 s default): fullrun5 ran 3600 s under it, the deadman disconnected the
# client at +15 min, the call counted the remaining 45 min as loss (24 % delivered = 900/3600) and the verdict
# still PASSed because a deadman disconnect is not a reconnect
deadman_cover "$DUR"
connect "$DEST" 20 || { verdict T23-sustained-soak FAIL "connect failed"; exit 1; }
node_sampler_start t23 5
in_client_bg "python3 /suite/probes/relprobe.py --host $TARGET_IP --port 8901 --rate-mbit 1.5 --duration $DUR --size 1200 --iface $WG_IFACE --out ${SUITE_RUN_IN_CLIENT}/t23-call"
t0=$(date +%s); rss0=$(in_client "ps -o rss= -C gnosis_vpn-worker | head -1" | tr -d ' '); log0=$(docker inspect -f '{{.LogPath}}' "$CLIENT" | xargs stat -c %s 2>/dev/null || echo 0)
n=0; while [ $(( $(date +%s) - t0 )) -lt "$DUR" ]; do
  sleep "$INTERVAL"; n=$((n+1))
  r=$(curl_down "$TARGET_IP" "$BYTES" "$CAP"); u=$(curl_up "$TARGET_IP" "$BYTES" "$CAP")
  rss=$(in_client "ps -o rss= -C gnosis_vpn-worker | head -1" | tr -d ' '); e=$(log_errors "$LOG_SINCE")
  emit_row T23-sustained-soak minute="$(( ( $(date +%s) - t0 ) / 60 ))" "down=$r" "up=$u" worker_rss_kb="${rss:-0}" "errors=$e"
  suite_log "soak +$(( ( $(date +%s) - t0 ) / 60 ))min: down $(json_get "$r" mbit) up $(json_get "$u" mbit) rss ${rss}kB reconnects $(json_get "$e" reconnects)"
done
sleep 5; node_sampler_stop; e=$(log_errors "$LOG_SINCE"); rss1=$(in_client "ps -o rss= -C gnosis_vpn-worker | head -1" | tr -d ' '); log1=$(docker inspect -f '{{.LogPath}}' "$CLIENT" | xargs stat -c %s 2>/dev/null || echo 0)
save_client_log t23 "$LOG_SINCE"; disconnect; call=$(cat "$SUITE_RUN/t23-call.json" 2>/dev/null || echo '{}')
# A reconnect can recreate the --rm client container, which replaces its log file: log1 < log0 then, and a
# naive delta reads negative (-3.19 MB/min was reported on 2026-09-18). Count from zero in that case.
lograte=$(python3 -c "l0,l1=$log0,$log1; d=l1-l0 if l1>=l0 else l1; print(round(d/1048576/max(1,$DUR/60),2))")
emit_row T23-sustained-soak kind=summary "errors=$e" rss_first="${rss0:-0}" rss_last="${rss1:-0}" log_mb_per_min="$lograte" "call=$call"
msg="${DUR}s: reconnects $(json_get "$e" reconnects) (tunnel-ping timeouts $(json_get "$e" ping_timeouts)), reassembly $(json_get "$e" reassembly_failed), rss ${rss0}→${rss1} kB, log ${lograte} MB/min, call loss $(json_get "$call" loss_pct)% stalls>5s $(json_get "$call" stalls_gt_5s)"
closs=$(json_get "$call" loss_pct); case "$closs" in ''|None) closs=100;; esac
python3 -c "import sys; sys.exit(0 if $(json_get "$e" reconnects)==0 and ${rss1:-0} < 2*max(${rss0:-1},1)+200000 and $lograte < $LOG_MB_MIN_MAX and $closs < $CALL_LOSS_MAX else 1)" && verdict T23-sustained-soak PASS "$msg" || verdict T23-sustained-soak FAIL "$msg"
exit $SUITE_FAILED
