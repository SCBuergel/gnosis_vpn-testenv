#!/usr/bin/env bash
# explore.sh — adversarial exploration of the Gnosis VPN tunnel. NOT part of the regression suite.
#
# Goal: find inputs that BREAK the tunnel, as distinct from plain overload. The distinction is the whole
# point of this file:
#   BROKE    the client tore the tunnel down (reconnect / interface recreated / worker restart) or the
#            session dropped to Disconnected, OR frames were lost to reassembly/decap errors — i.e. a
#            failure of the transport, not of capacity.
#   OVERLOAD packets were lost or delayed but the tunnel stayed up and no transport error was logged. This
#            is EXPECTED when you push more than the path carries and is NOT a finding.
#   CLEAN    delivered within tolerance, tunnel intact.
#
# The liveness-ping artifact (client pings 10.128.0.1, server holds only 10.129.0.1) is assumed FIXED here by
# the SERVER_PING_ALIAS stopgap: every scenario checks ping_timeouts, and a reconnect with ~3 ping timeouts
# each AT IDLE is the artifact, not a finding. A reconnect that appears only under load, or with 0 ping
# timeouts, is real.
#
# It reuses the suite's helpers (connect/log_errors/curl/target) and the suite's probes already mounted at
# /suite/probes in the client, but writes to its own output dir and is never listed in run.sh.
#
# Usage:  exploration/explore.sh <scenario> [k=v ...]     exploration/explore.sh list
set -uo pipefail
export LC_ALL=C
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${TESTENV_DIR:=$(cd "$HERE/.." && pwd)}"
SCEN="${1:-list}"; shift || true
if [ "$SCEN" = list ] || [ "$SCEN" = -h ] || [ "$SCEN" = --help ]; then
  echo "scenarios: overload-repeat overload-scar surb-collapse pure-download idle-resume mtu-frag reorder churn burst-idle bidir soak"
  echo "usage: exploration/explore.sh <scenario> [KEY=value ...]"; exit 0
fi
for kv in "$@"; do export "$kv"; done          # k=v overrides on the command line

export SUITE_OUT_DIR="${EXPLORE_OUT:-$HERE/runs}"
export SUITE_RUN="${SUITE_OUT_DIR}/$(date -u +%Y%m%dT%H%M%SZ)-${SCEN}"
: "${SURB_RAMP_WAIT:=25}"; export SURB_RAMP_WAIT
: "${DEADMAN:=1200}"; export DEADMAN            # raised per scenario for soak
# shellcheck disable=SC1090
source "${TESTENV_DIR}/scripts/suite/lib.sh"
set +e                                          # exploration continues across a broken scenario
suite_init
mkdir -p "${SUITE_RUN}"
LOG="${SUITE_RUN}/explore.log"
say() { printf '%s  %s\n' "$(date -u +%T)" "$*" | tee -a "$LOG" >&2; }

# ---- classification -------------------------------------------------------
# verdict_of SINCE PROBE_JSON PHASE_UNDER_LOAD(0|1) -> prints "BROKE|OVERLOAD|CLEAN <detail>"
verdict_of() {
  local since=$1 pj=${2:-'{}'} under_load=${3:-1}
  local e rc pt rf ds ns loss reb out conn
  e=$(log_errors "$since")
  rc=$(json_get "$e" reconnects); pt=$(json_get "$e" ping_timeouts)
  rf=$(json_get "$e" reassembly_failed); ds=$(json_get "$e" decap_stalled); ns=$(json_get "$e" no_surb)
  loss=$(json_get "$pj" loss_pct); reb=$(json_get "$pj" rebinds); out=$(json_get "$pj" outage_total_s)
  [ -z "$loss" ] && loss="n/a"; [ -z "$reb" ] && reb=0; [ -z "$out" ] && out="n/a"
  client_is_connected && conn=up || conn=DOWN
  local tag detail
  detail="reconnects=$rc ping_timeouts=$pt reassembly=$rf decap_stalled=$ds no_surb=$ns rebinds=$reb outage_s=$out loss%=$loss session=$conn"
  if [ "$conn" = DOWN ]; then tag=BROKE
  elif [ "${rf:-0}" -gt 0 ] || [ "${ds:-0}" -gt 0 ]; then tag=BROKE
  elif [ "${rc:-0}" -gt 0 ]; then tag=BROKE
  elif [ "${reb:-0}" -gt 0 ]; then tag=BROKE          # interface was recreated = a reconnect the log line was missed
  elif [ "$loss" != "n/a" ] && awk "BEGIN{exit !($loss>5)}"; then tag=OVERLOAD
  else tag=CLEAN
  fi
  # a reconnect preceded by ~3 ping timeouts each AND at idle is the known liveness artifact, flag it
  if [ "${rc:-0}" -gt 0 ] && [ "$under_load" = 0 ] && [ "${pt:-0}" -ge $((3*rc)) ]; then
    detail="$detail  [looks like the liveness-ping artifact: is SERVER_PING_ALIAS set?]"
  fi
  printf '%s %s\n' "$tag" "$detail"
}

RES="${SUITE_RUN}/results.tsv"
record_result() { printf '%s\t%s\t%s\n' "$SCEN" "$1" "$2" >> "$RES"; say "VERDICT[$1] $2"; }

require_target || { say "target not up"; exit 1; }
TIP="$TARGET_IP"
# The client mounts the SUITE's out dir at /suite-out, which is NOT this exploration run dir, so probes must
# write to a path that exists inside the client. Use a client-local tmp dir and read results from probe stdout.
COUT=/tmp/explore
in_client "mkdir -p $COUT"
say "scenario=$SCEN run=$SUITE_RUN target=$TIP client=$CLIENT dest=$DEST ramp_wait=$SURB_RAMP_WAIT deadman=$DEADMAN"
say "server wggvpn: $(docker exec gnosis_vpn-server-0 ip -4 -o addr show dev wggvpn 2>/dev/null | awk '{print $4}' | tr '\n' ' ')"

# run one relprobe echo arm inside the client; echoes the summary json
echo_arm() { # rate dur size label
  local r=$1 dur=$2 sz=$3 lbl=$4
  in_client "python3 /suite/probes/relprobe.py --host $TIP --port 8901 --rate-mbit $r --duration $dur --size $sz --iface $WG_IFACE --out ${COUT}/${lbl}" 2>/dev/null | tail -1
}
stream_arm() { # mode rate dur size label
  local m=$1 r=$2 dur=$3 sz=$4 lbl=$5
  in_client "python3 /suite/probes/streamprobe.py --mode $m --host $TIP --port 8902 --rate-mbit $r --duration $dur --size $sz --iface $WG_IFACE --out ${COUT}/${lbl}.json" 2>/dev/null | tail -1
}

# ===========================================================================
# scenarios
# ===========================================================================

# 1. pure-download: return-path (SURB) pressure. The client provides SURBs for server->client traffic; a
#    sustained download with no client->server traffic is the maximum SURB demand. Rising rate. A download
#    that just slows or loses is OVERLOAD; one that RECONNECTS or stalls the tunnel is a break.
scen_pure_download() {
  : "${RATES:=2 4 6 8 10}" "${DUR:=$(q 120 120)}" "${SIZE:=1200}"
  for r in $RATES; do
    connect "$DEST" 0 || { record_result "dl-${r}Mbit" "BROKE connect failed"; continue; }
    local since=$LOG_SINCE
    say "pure-download ${r} Mbit/s for ${DUR}s (size $SIZE)"
    local j; j=$(stream_arm dl "$r" "$DUR" "$SIZE" "dl-$r")
    save_client_log "dl-$r" "$since"
    record_result "dl-${r}Mbit" "$(verdict_of "$since" "$j" 1) | probe=$(printf '%s' "$j" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("recv=%s/%s loss%%=%s stalls>5s=%s"%(d.get("recv"),d.get("sent"),d.get("loss_pct"),d.get("stalls_gt_5s")))' 2>/dev/null)"
    disconnect
  done
}

# 2. idle-resume: does an idle tunnel survive a resume? Connect, idle N seconds (past SURB expiry), then one
#    download. With the liveness ping healthy, a reconnect/starve here is a real product weakness (an idle VPN
#    that cannot resume without tearing down). Sweeps idle length.
scen_idle_resume() {
  : "${IDLES:=30 60 120 240}" "${DUR:=60}" "${SIZE:=1200}" "${RATE:=4}"
  export RAMP_WAIT_OPT_OUT=1
  for idle in $IDLES; do
    connect "$DEST" 0 || { record_result "idle-${idle}s" "BROKE connect failed"; continue; }
    local since=$LOG_SINCE
    say "idle-resume: idle ${idle}s then ${RATE} Mbit/s dl ${DUR}s"
    sleep "$idle"
    local mid; mid=$(utc_now)
    local j; j=$(stream_arm dl "$RATE" "$DUR" "$SIZE" "resume-$idle")
    save_client_log "idle-$idle" "$since"
    # classify on the whole window; note idle-phase reconnects separately
    local idle_e; idle_e=$(log_errors "$since" | python3 -c 'import sys,json;print(json.load(sys.stdin)["reconnects"])')
    record_result "idle-${idle}s" "$(verdict_of "$mid" "$j" 1) | idle_phase_reconnects=$idle_e | probe=$(printf '%s' "$j" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("recv=%s/%s loss%%=%s"%(d.get("recv"),d.get("sent"),d.get("loss_pct")))' 2>/dev/null)"
    disconnect
  done
  unset RAMP_WAIT_OPT_OUT
}

# 3. mtu-frag: datagrams larger than the tunnel MTU force IP fragmentation and reassembly on the far side.
#    A tunnel that drops oversize packets is OVERLOAD; reassembly_failed / decap_stalled / a reconnect is a
#    break. Uses the echo probe so both directions traverse fragmentation.
scen_mtu_frag() {
  : "${SIZES:=1400 1600 2000 4000 8000 20000 60000}" "${DUR:=$(q 60 45)}" "${RATE:=1.5}"
  local mtu; mtu=$(in_client "cat /sys/class/net/\$(ls /sys/class/net|grep -m1 ^wg)/mtu" 2>/dev/null)
  say "tunnel MTU=$mtu"
  connect "$DEST" 0 || { record_result "mtu-frag" "BROKE connect failed"; return; }
  local since0=$LOG_SINCE
  for sz in $SIZES; do
    local since; since=$(utc_now)
    say "mtu-frag size=$sz (MTU $mtu) at ${RATE} Mbit/s ${DUR}s"
    local j; j=$(echo_arm "$RATE" "$DUR" "$sz" "frag-$sz")
    record_result "frag-${sz}B" "$(verdict_of "$since" "$j" 1) | probe=$(printf '%s' "$j" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("recv=%s/%s loss%%=%s rtt_p50=%s"%(d.get("recv"),d.get("sent"),d.get("loss_pct"),(d.get("rtt_ms") or {}).get("p50")))' 2>/dev/null)"
    ! client_is_connected && { say "session dropped at size=$sz — stopping sweep"; break; }
  done
  save_client_log "mtu-frag" "$since0"
  disconnect
}

# 4. reorder-dup: netem on lo reorders / duplicates packets between relays (not loss). The decapsulator must
#    tolerate reordering; if it stalls (DecapStalled) or the client reconnects, that is a break.
scen_reorder() {
  : "${DUR:=$(q 90 60)}" "${RATE:=3}" "${SIZE:=1200}"
  connect "$DEST" 0 || { record_result "reorder" "BROKE connect failed"; return; }
  local since=$LOG_SINCE
  # reorder ~15% with 40ms spread + 1% duplication on all lo traffic (both relays' P2P rides lo here)
  tc qdisc del dev lo root 2>/dev/null
  tc qdisc add dev lo root netem delay 20ms reorder 25% 50% duplicate 1% 2>/dev/null && say "netem: reorder 25%/delay20ms/dup1% on lo" || say "netem apply FAILED (need root)"
  local j; j=$(echo_arm "$RATE" "$DUR" "$SIZE" "reorder")
  tc qdisc del dev lo root 2>/dev/null; say "netem cleared"
  save_client_log "reorder" "$since"
  record_result "reorder-dup" "$(verdict_of "$since" "$j" 1) | probe=$(printf '%s' "$j" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("recv=%s/%s loss%%=%s dup=%s stalls>5s=%s"%(d.get("recv"),d.get("sent"),d.get("loss_pct"),d.get("dup"),d.get("stalls_gt_5s")))' 2>/dev/null)"
  disconnect
}

# 5. churn: rapid connect / short-transfer / disconnect cycles. Tests state leaks — a channel left
#    PendingToClose, a wedged worker, a SURB pool that never recovers. A cycle that fails to connect or a
#    transfer that fails on a later cycle after early cycles passed is a break.
scen_churn() {
  : "${CYCLES:=12}" "${HOLD:=8}" "${RATE:=4}" "${GAP:=2}"
  local fails=0 first_fail=""
  for c in $(seq 1 "$CYCLES"); do
    local since; since=$(utc_now)
    if ! connect "$DEST" 0; then fails=$((fails+1)); [ -z "$first_fail" ] && first_fail="connect@$c"; say "cycle $c: connect FAILED"; continue; fi
    local d; d=$(curl_down "$TIP" 3000000 "$HOLD")
    local ok; ok=$(json_get "$d" complete); local mbit; mbit=$(json_get "$d" mbit)
    say "cycle $c: connect ok, dl complete=$ok ${mbit}Mbit/s"
    [ "$ok" != True ] && { fails=$((fails+1)); [ -z "$first_fail" ] && first_fail="transfer@$c"; }
    disconnect
    sleep "$GAP"
  done
  record_result "churn-${CYCLES}x" "$([ "$fails" = 0 ] && echo CLEAN || echo BROKE) failed_cycles=$fails first_fail=${first_fail:-none}"
}

# 6. burst-idle: oscillate heavy burst + idle repeatedly on ONE session. Stresses SURB ramp-up/decay
#    repeatedly; watches for accumulation into a reconnect or a burst that never recovers.
scen_burst_idle() {
  : "${ROUNDS:=8}" "${BURST:=15}" "${IDLE:=20}" "${RATE:=6}" "${SIZE:=1200}"
  connect "$DEST" 0 || { record_result "burst-idle" "BROKE connect failed"; return; }
  local since=$LOG_SINCE worst="" any_broke=0
  for c in $(seq 1 "$ROUNDS"); do
    local rs; rs=$(utc_now)
    local j; j=$(echo_arm "$RATE" "$BURST" "$SIZE" "burst-$c")
    local loss; loss=$(json_get "$j" loss_pct); local reb; reb=$(json_get "$j" rebinds)
    say "round $c: burst ${RATE}Mbit/${BURST}s loss=${loss}% rebinds=${reb}; idle ${IDLE}s"
    [ "${reb:-0}" -gt 0 ] && any_broke=1
    ! client_is_connected && { any_broke=1; say "round $c: session DOWN after burst"; break; }
    sleep "$IDLE"
  done
  save_client_log "burst-idle" "$since"
  record_result "burst-idle-${ROUNDS}x" "$(verdict_of "$since" '{}' 1) | any_rebind_or_down=$any_broke"
  disconnect
}

# 7. bidir: simultaneous upload + download at a moderate, non-overload rate, sustained. Contention on one
#    session; a break is a reconnect or one direction collapsing while the other lives.
scen_bidir() {
  : "${DUR:=$(q 120 90)}" "${RATE:=3}" "${SIZE:=1200}"
  connect "$DEST" 0 || { record_result "bidir" "BROKE connect failed"; return; }
  local since=$LOG_SINCE
  say "bidir: ul+dl ${RATE} Mbit/s each for ${DUR}s"
  local jd ju
  in_client "python3 /suite/probes/streamprobe.py --mode dl --host $TIP --port 8902 --rate-mbit $RATE --duration $DUR --size $SIZE --iface $WG_IFACE --out ${COUT}/bidir-dl.json" >/dev/null 2>&1 &
  local dlpid=$!
  ju=$(stream_arm ul "$RATE" "$DUR" "$SIZE" "bidir-ul")
  wait "$dlpid" 2>/dev/null
  jd=$(in_client "cat ${COUT}/bidir-dl.json" 2>/dev/null)
  save_client_log "bidir" "$since"
  record_result "bidir" "$(verdict_of "$since" "$jd" 1) | dl=$(printf '%s' "$jd" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("recv=%s/%s loss%%=%s"%(d.get("recv"),d.get("sent"),d.get("loss_pct")))' 2>/dev/null) ul=$(printf '%s' "$ju" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("recv=%s/%s loss%%=%s"%(d.get("recv"),d.get("sent"),d.get("loss_pct")))' 2>/dev/null)"
  disconnect
}

# 8. soak: moderate sustained echo for a long time. Re-tests the old "soak dead at +21 min" now that the
#    liveness ping is healthy. A reconnect / interface recreation / session drop anywhere in the window is a
#    break; steady loss is not.
scen_soak() {
  : "${DUR:=1800}" "${RATE:=1.5}" "${SIZE:=1200}"
  export DEADMAN=$((DUR+300)); disarm_deadman 2>/dev/null
  connect "$DEST" 0 || { record_result "soak" "BROKE connect failed"; return; }
  local since=$LOG_SINCE
  say "soak: ${RATE} Mbit/s echo for ${DUR}s (deadman $DEADMAN)"
  local j; j=$(echo_arm "$RATE" "$DUR" "$SIZE" "soak")
  save_client_log "soak" "$since"
  record_result "soak-${DUR}s" "$(verdict_of "$since" "$j" 1) | probe=$(printf '%s' "$j" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("recv=%s/%s loss%%=%s stalls>5s=%s worst=%s"%(d.get("recv"),d.get("sent"),d.get("loss_pct"),d.get("stalls_gt_5s"),(d.get("worst_stalls") or [["",0]])[0][1]))' 2>/dev/null)"
  disconnect
}

# 9. surb-collapse: isolate the return-path relayer-diversity collapse + SURB expiry storm found in soak/T23.
#    MODE=load runs a steady echo; MODE=idle is the control (no traffic). Samples the onset counters per minute
#    with bounded per-minute log reads. Establishes WHEN diversity collapses and whether it is load-triggered.
scen_surb_collapse() {
  : "${MODE:=load}" "${DUR:=900}" "${RATE:=1.5}" "${SIZE:=1200}"
  export DEADMAN=$((DUR+300)); export RAMP_WAIT_OPT_OUT=1
  connect "$DEST" 0 || { record_result "surb-collapse-$MODE" "BROKE connect failed"; return; }
  local since=$LOG_SINCE
  say "surb-collapse MODE=$MODE ${DUR}s rate=${RATE} size=${SIZE}"
  [ "$MODE" = load ] && in_client "nohup python3 /suite/probes/relprobe.py --host $TIP --port 8901 --rate-mbit $RATE --duration $DUR --size $SIZE --iface $WG_IFACE --out ${COUT}/surb-echo >/dev/null 2>&1 & echo started"
  local t0; t0=$(date +%s); local m=0 last=$since dc=0 es=0 er=0 pt=0 rc=0 onset="" chunk=/tmp/explore-chunk.log
  say "min div_collapse evict_surb evict_reply ping_to reconnects"
  while [ $(( $(date +%s) - t0 )) -lt "$DUR" ]; do
    sleep 60; m=$((m+1)); local now; now=$(utc_now)
    docker logs --since "$last" --until "$now" "$CLIENT" > "$chunk" 2>&1; last=$now
    dc=$((dc+$(grep -ac "diversity collapsed to a single relayer" "$chunk")))
    es=$((es+$(grep -ac "evicting surb" "$chunk")))
    er=$((er+$(grep -ac "evicting reply opener" "$chunk")))
    pt=$((pt+$(grep -ac "TunnelPingResult: Error(Ping timed out)" "$chunk")))
    rc=$((rc+$(grep -ac "exceeded max failures - reconnecting" "$chunk")))
    say "$(printf '%3d %6s %6s %6s %6s %6s' "$m" "$dc" "$es" "$er" "$pt" "$rc")"
    [ -z "$onset" ] && [ "${dc:-0}" -gt 0 ] && onset=$m
    client_is_connected || { say "session DOWN at min $m"; break; }
  done
  save_client_log "surb-collapse-$MODE" "$since"
  local j=""; [ "$MODE" = load ] && j=$(in_client "cat ${COUT}/surb-echo.json 2>/dev/null")
  record_result "surb-collapse-$MODE" "$(verdict_of "$since" "$j" 1) | onset_min=${onset:-none} div_collapse=$dc evict_surb=$es evict_reply=$er | probe=$(printf '%s' "$j" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("recv=%s/%s loss%%=%s outage_s=%s"%(d.get("recv"),d.get("sent"),d.get("loss_pct"),d.get("outage_total_s")))' 2>/dev/null)"
  disconnect; unset RAMP_WAIT_OPT_OUT
}

# 10. overload-scar: does a short CBR OVERLOAD burst leave the tunnel persistently degraded for normal traffic
#     afterwards? On a fresh stack, sustained 1.5 Mbit/s is clean; the soak only broke after heavy prior stress.
#     Sequence on ONE session: baseline echo -> overload blast (dl at BLAST_RATE) -> recovery echo at normal rate.
#     If recovery loss/reconnects >> baseline, the overload scarred the tunnel (a real break, not transient loss).
scen_overload_scar() {
  : "${BLAST_RATE:=15}" "${BLAST_DUR:=120}" "${RECOVER_RATE:=1.5}" "${RECOVER_DUR:=600}" "${BASE_DUR:=90}" "${SIZE:=1200}"
  export DEADMAN=$((BASE_DUR+BLAST_DUR+RECOVER_DUR+400))
  connect "$DEST" 0 || { record_result overload-scar "BROKE connect failed"; return; }
  local since=$LOG_SINCE
  local sb; sb=$(utc_now); say "baseline echo ${RECOVER_RATE} Mbit/s ${BASE_DUR}s"
  local base; base=$(echo_arm "$RECOVER_RATE" "$BASE_DUR" "$SIZE" "scar-pre")
  record_result overload-scar-baseline "$(verdict_of "$sb" "$base" 1) | probe=$(printf '%s' "$base" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("loss%%=%s recv=%s/%s"%(d.get("loss_pct"),d.get("recv"),d.get("sent")))' 2>/dev/null)"
  local sbl; sbl=$(utc_now); say "BLAST dl ${BLAST_RATE} Mbit/s ${BLAST_DUR}s (overload)"
  local blast; blast=$(stream_arm dl "$BLAST_RATE" "$BLAST_DUR" "$SIZE" "scar-blast")
  record_result overload-scar-blast "$(verdict_of "$sbl" "$blast" 1) | probe=$(printf '%s' "$blast" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("loss%%=%s recv=%s/%s"%(d.get("loss_pct"),d.get("recv"),d.get("sent")))' 2>/dev/null)"
  local sr; sr=$(utc_now); say "RECOVER echo ${RECOVER_RATE} Mbit/s ${RECOVER_DUR}s"
  local rec; rec=$(echo_arm "$RECOVER_RATE" "$RECOVER_DUR" "$SIZE" "scar-recover")
  save_client_log overload-scar "$since"
  record_result overload-scar-recover "$(verdict_of "$sr" "$rec" 1) | probe=$(printf '%s' "$rec" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("loss%%=%s recv=%s/%s stalls>5s=%s outage_s=%s"%(d.get("loss_pct"),d.get("recv"),d.get("sent"),d.get("stalls_gt_5s"),d.get("outage_total_s")))' 2>/dev/null)"
  disconnect
}

# 11. overload-repeat: does REPEATED overload accumulate into a state the tunnel stops recovering from? A single
#     15 Mbit/s blast breaks then self-heals (overload-scar). This runs N blast+recover cycles on ONE session and
#     watches whether the recovery arm degrades across cycles or a cycle fails to recover / the session stays down.
scen_overload_repeat() {
  : "${CYCLES:=8}" "${BLAST_RATE:=15}" "${BLAST_DUR:=120}" "${REC_RATE:=1.5}" "${REC_DUR:=120}" "${SIZE:=1200}"
  export DEADMAN=$((CYCLES*(BLAST_DUR+REC_DUR)+600))
  connect "$DEST" 0 || { record_result overload-repeat "BROKE connect failed"; return; }
  local since=$LOG_SINCE c
  for c in $(seq 1 "$CYCLES"); do
    local b; b=$(stream_arm dl "$BLAST_RATE" "$BLAST_DUR" "$SIZE" "rep-blast-$c")
    local sr; sr=$(utc_now); local r; r=$(echo_arm "$REC_RATE" "$REC_DUR" "$SIZE" "rep-rec-$c")
    record_result "cycle-$c" "recover $(verdict_of "$sr" "$r" 1) | blast_loss=$(json_get "$b" loss_pct)% recover=$(printf '%s' "$r" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("loss%%=%s recv=%s/%s rebinds=%s outage_s=%s"%(d.get("loss_pct"),d.get("recv"),d.get("sent"),d.get("rebinds"),d.get("outage_total_s")))' 2>/dev/null)"
    client_is_connected || { say "session DOWN after cycle $c — stopping"; break; }
  done
  save_client_log overload-repeat "$since"
  disconnect
}

case "$SCEN" in
  overload-repeat) scen_overload_repeat ;;
  overload-scar) scen_overload_scar ;;
  surb-collapse) scen_surb_collapse ;;
  pure-download) scen_pure_download ;;
  idle-resume)   scen_idle_resume ;;
  mtu-frag)      scen_mtu_frag ;;
  reorder)       scen_reorder ;;
  churn)         scen_churn ;;
  burst-idle)    scen_burst_idle ;;
  bidir)         scen_bidir ;;
  soak)          scen_soak ;;
  list|*) echo "scenarios: overload-repeat overload-scar surb-collapse pure-download idle-resume mtu-frag reorder churn burst-idle bidir soak"; exit 0 ;;
esac

say "=== $SCEN done ==="
echo "== results =="; cat "$RES" 2>/dev/null
