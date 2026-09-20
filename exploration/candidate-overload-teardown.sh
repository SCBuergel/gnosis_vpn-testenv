#!/usr/bin/env bash
# CANDIDATE regression test (NOT in the suite; lives in exploration/, git-excluded) for Finding 1 in FINDINGS.md:
#
#   Under constant-bitrate overload the client must SHED LOAD (drop packets), not TEAR THE TUNNEL DOWN.
#
# On the 2026-09-20 stack this fails: a 120 s 15 Mbit/s CBR download (~1.5x the ~10-13 Mbit/s path) drives the
# return path to starvation, the periodic tunnel ping times out 3x and the watchdog reconnects (~25 s outage),
# with tens of thousands of `failed to reassemble frame ... expired or discarded`. High loss under overload is
# expected and does NOT fail this test; a reconnect / interface teardown does. The tunnel self-heals afterwards
# (see FINDINGS.md), so this gate is about the teardown, not lasting damage.
#
# It reproduces in isolation: run it against a freshly settled stack and it fails at BLAST_RATE=15 while a
# baseline 1.5 Mbit/s arm on the same session is clean.
#
# Knobs: BLAST_RATE=15 (Mbit/s, set above measured path capacity) BLAST_DUR=120 SIZE=1200
#        BASE_RATE=1.5 BASE_DUR=60 (the sanity baseline)  REASM_MAX=100 (session-reassembly failures allowed)
#
# Run:  cd /root/testenv/gnosis_vpn-testenv && exploration/candidate-overload-teardown.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SUITE_OUT_DIR="${EXPLORE_OUT:-$HERE/runs}"
export SUITE_RUN="${SUITE_OUT_DIR}/$(date -u +%Y%m%dT%H%M%SZ)-candidate-overload-teardown"
# shellcheck disable=SC1090
source "$(cd "$HERE/.." && pwd)/scripts/suite/lib.sh"
set +e
suite_kind gate
: "${BLAST_RATE:=15}" "${BLAST_DUR:=120}" "${SIZE:=1200}" "${BASE_RATE:=1.5}" "${BASE_DUR:=60}" "${REASM_MAX:=100}"
suite_init; require_target || { verdict candidate-overload-teardown SKIP "target not up"; exit 0; }
TIP="$TARGET_IP"; COUT=/tmp/explore; in_client "mkdir -p $COUT"

connect "$DEST" 0 || { verdict candidate-overload-teardown FAIL "connect failed"; exit 1; }

# baseline: a normal-rate arm on this session must be healthy, else the stack is already degraded and the
# overload result would be uninterpretable (report SKIP rather than a false FAIL)
base_since=$(utc_now)
base=$(in_client "python3 /suite/probes/relprobe.py --host $TIP --port 8901 --rate-mbit $BASE_RATE --duration $BASE_DUR --size $SIZE --iface $WG_IFACE --out ${COUT}/cand-base" 2>/dev/null | tail -1)
be=$(log_errors "$base_since"); bloss=$(json_get "$base" loss_pct)
if [ "$(json_get "$be" reconnects)" != 0 ] || awk "BEGIN{exit !(${bloss:-100}>5)}"; then
  save_client_log candidate-baseline "$base_since"
  verdict candidate-overload-teardown SKIP "baseline unhealthy (loss ${bloss}% reconnects $(json_get "$be" reconnects)) — restart the stack before trusting this test"
  disconnect; exit 0
fi

# overload: push CBR well above capacity for BLAST_DUR
blast_since=$(utc_now)
blast=$(in_client "python3 /suite/probes/streamprobe.py --mode dl --host $TIP --port 8902 --rate-mbit $BLAST_RATE --duration $BLAST_DUR --size $SIZE --iface $WG_IFACE --out ${COUT}/cand-blast.json" 2>/dev/null | tail -1)
e=$(log_errors "$blast_since"); save_client_log candidate-blast "$blast_since"
rc=$(json_get "$e" reconnects); reasm=$(json_get "$e" reassembly_failed); pt=$(json_get "$e" ping_timeouts)
reb=$(json_get "$blast" rebinds); loss=$(json_get "$blast" loss_pct); outage=$(json_get "$blast" outage_total_s)
disconnect

msg="${BLAST_RATE} Mbit/s CBR x${BLAST_DUR}s: reconnects ${rc} (tunnel-ping timeouts ${pt}), probe rebinds ${reb}, outage ${outage}s, reassembly-failures ${reasm}, loss ${loss}% (loss under overload is allowed; a reconnect/teardown is not); baseline ${BASE_RATE} Mbit/s was ${bloss}% clean"
# PASS = the tunnel shed load without tearing down: no reconnect, no interface rebind, reassembly bounded.
if [ "${rc:-1}" = 0 ] && [ "${reb:-1}" = 0 ] && [ "${reasm:-999999}" -le "$REASM_MAX" ]; then
  verdict candidate-overload-teardown PASS "$msg"
else
  verdict candidate-overload-teardown FAIL "$msg"
fi
