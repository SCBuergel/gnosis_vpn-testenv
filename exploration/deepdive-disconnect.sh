#!/usr/bin/env bash
# deepdive-disconnect.sh — single instrumented reproduction of the OVERLOAD DISCONNECT (FINDINGS.md Finding 1):
# a CBR download above path capacity makes the client tear the tunnel down. This pins the causal chain by
# sampling, throughout each arm: WireGuard rx/tx, the client's active-session SURB buffer estimate, and the
# exit's SURB balancer buffer/target/rate — then lining them up against the millisecond ping/reconnect log.
#
# Three arms, each on its own fresh session:
#   baseline  1.5 Mbit/s echo, 30 s      — control: must be clean
#   download  DL_RATE Mbit/s DL, DUR s    — the disconnect
#   upload    DL_RATE Mbit/s UL, DUR s    — direction control: does saturating the OTHER way also disconnect?
#
# Env: DL_RATE=15 DUR=180 SIZE=1200 SAMPLE_INT=1
# Run: cd /root/testenv/gnosis_vpn-testenv && exploration/deepdive-disconnect.sh   (stack up + freshly settled)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SUITE_OUT_DIR="${EXPLORE_OUT:-$HERE/runs}"
export SUITE_RUN="${SUITE_OUT_DIR}/$(date -u +%Y%m%dT%H%M%SZ)-deepdive-disconnect"
export DEADMAN="${DEADMAN:-600}"
# shellcheck disable=SC1090
source "$(cd "$HERE/.." && pwd)/scripts/suite/lib.sh"
set +e
: "${DL_RATE:=15}" "${DUR:=180}" "${SIZE:=1200}" "${SAMPLE_INT:=1}"
suite_init; require_target || { echo "target not up"; exit 1; }
TIP="$TARGET_IP"; COUT=/tmp/explore; in_client "mkdir -p $COUT"
LOG="$SUITE_RUN/deepdive.log"; say() { printf '%s  %s\n' "$(date -u +%T)" "$*" | tee -a "$LOG"; }

# exit node (node-0) api url + token for the SURB sampler
CS=$(cluster_status); N0URL=$(echo "$CS" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nodes"][0]["api_url"])')
N0TOK=$(echo "$CS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["nodes"][0].get("api_token") or d.get("api_token") or "")')
say "deepdive run=$SUITE_RUN target=$TIP dl_rate=$DL_RATE dur=$DUR exit=$N0URL"
say "server wggvpn: $(docker exec gnosis_vpn-server-0 ip -4 -o addr show dev wggvpn 2>/dev/null | awk '{print $4}' | tr '\n' ' ')"

SAMPLE_PID=""
sample_start() { # name
  python3 "$HERE/sampler.py" --client "$CLIENT" --wg "$WG_IFACE" --node-url "$N0URL" --node-token "$N0TOK" \
    --interval "$SAMPLE_INT" --out "$SUITE_RUN/sample-$1.csv" >/dev/null 2>&1 &
  SAMPLE_PID=$!
}
sample_stop() { [ -n "$SAMPLE_PID" ] && kill "$SAMPLE_PID" 2>/dev/null; SAMPLE_PID=""; }

arm() { # name mode rate dur   (mode: echo|dl|ul)
  local name=$1 mode=$2 rate=$3 dur=$4
  connect "$DEST" 0 || { say "[$name] connect FAILED"; return 1; }
  local since=$LOG_SINCE
  sample_start "$name"
  say "[$name] $mode ${rate} Mbit/s ${dur}s (session up, sampling every ${SAMPLE_INT}s)"
  local j
  if [ "$mode" = echo ]; then
    j=$(in_client "python3 /suite/probes/relprobe.py --host $TIP --port 8901 --rate-mbit $rate --duration $dur --size $SIZE --iface $WG_IFACE --out ${COUT}/dd-$name" 2>/dev/null | tail -1)
  else
    j=$(in_client "python3 /suite/probes/streamprobe.py --mode $mode --host $TIP --port 8902 --rate-mbit $rate --duration $dur --size $SIZE --iface $WG_IFACE --out ${COUT}/dd-$name.json" 2>/dev/null | tail -1)
  fi
  sample_stop
  save_client_log "$name" "$since"
  echo "$j" > "$SUITE_RUN/probe-$name.json"
  local e; e=$(log_errors "$since")
  say "[$name] reconnects=$(json_get "$e" reconnects) ping_timeouts=$(json_get "$e" ping_timeouts) reassembly=$(json_get "$e" reassembly_failed) | probe loss%=$(json_get "$j" loss_pct) recv=$(json_get "$j" recv)/$(json_get "$j" sent) rebinds=$(json_get "$j" rebinds) outage_s=$(json_get "$j" outage_total_s)"
  disconnect
  # per-arm correlated timeline
  python3 "$HERE/analyze-deepdive.py" "$SUITE_RUN" "$name" 2>/dev/null | tee -a "$LOG"
}

arm baseline echo 1.5 30
arm download dl "$DL_RATE" "$DUR"
arm upload   ul "$DL_RATE" "$DUR"
say "=== deepdive done. run dir: $SUITE_RUN ==="
