#!/usr/bin/env bash
#
# run.sh - run the regression suite (docs/regression-catalogue.md) against the live stack.
#
#   run.sh [--very-fast] [--fast] [--only tNN,tNN] [--skip tNN,...] [--run-id ID] [--ref-cell NAME]
#
# THERE IS ONE RUN. Every test runs, in one order, every time — there are no profiles to pick between, because
# a profile you can choose is a profile someone forgets to choose, and coverage then depends on which name was
# typed. Make the run shorter instead:
#   --very-fast        aggressive overrides on every long step; targets well under 30 minutes for the whole suite
#   --fast       the catalogue's own shortened durations (less aggressive than --very-fast)
#   --only/--skip ad hoc, for working on one test
#   T<NN>_<VAR>   per-test knob, e.g. T09_STREAM_S=10 T23_DUR=150 — overrides --very-fast
#
# Runbook items (fleet, investigation, tooling: t25 t26 t27 t28 t29 t30 t31 t32) are not in the run; run them
# explicitly with `just test tNN`.
#
# Scoring. Only a *gate* can fail a run. The suite tests ONE stack at a time (versions are run sequentially),
# so host-dependent numbers are scored against the last stored value for that metric — the previous run,
# usually the previous version — inside T03-repeatability-baseline's band. Without a T03-repeatability-baseline record for this stack the suite still runs
# and records everything but refuses to score, and says so. t03 runs third in every run, so the record exists from
# the first full run on; `--only t01,t02,t03` creates it alone.
# --ref-cell exists only for the explicit A/B path (a newly added test with no stored history yet).
#
# Results: SUITE_OUT_DIR/<run-id>/ (rows.jsonl, verdicts.jsonl, provenance.json, summary.csv, logs/, samples/).
set -euo pipefail
export LC_ALL=C
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# a bare leading word is accepted and ignored so old call sites (`run.sh all`) keep working
case "${1:-}" in all|full|suite|"") [ $# -gt 0 ] && shift || true;; -*) :;;
  *) echo "run.sh takes no profile - there is one run. Use --only ${1} to run just that test." >&2; exit 2;; esac
ONLY=""; SKIP=""; RUN_ID=""; VERY_FAST=0
while [ $# -gt 0 ]; do case "$1" in
  --very-fast) VERY_FAST=1;; --fast) export SUITE_FAST=1;; --only) ONLY="$2"; shift;; --skip) SKIP="$2"; shift;;
  --run-id) RUN_ID="$2"; shift;; --ref-cell) export SUITE_REF_CELL="$2"; shift;;
  --help|-h) sed -n '3,20p' "$0"; exit 0;; *) echo "unknown arg $1" >&2; exit 2;; esac; shift; done
: "${SUITE_OUT_DIR:=/tmp/gnosis_vpn-testenv-suite}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)${SUITE_CELL:+-$SUITE_CELL}}"
export SUITE_RUN="${SUITE_OUT_DIR}/${RUN_ID}"
# A run id is a directory, and every test appends to it. Reusing one blends two runs into one verdicts file with
# no way to tell them apart afterwards (it happened on 2026-09-18: an OOM-killed run and its clean re-run shared an
# id). Refuse rather than merge.
if [ -s "${SUITE_RUN}/verdicts.jsonl" ]; then
  echo "run id '${RUN_ID}' already holds results in ${SUITE_RUN}; choose another --run-id or move that directory" >&2
  exit 2
fi
mkdir -p "${SUITE_RUN}"; ln -sfn "${SUITE_RUN}" "${SUITE_OUT_DIR}/latest"

# ONE list, one order. Placement is load-bearing in three places:
#   - t03 runs third so this run is scored against a band measured on this stack now, not a stale one;
#   - t05 runs fifth, right after the t04 throughput reference, because its loaded-RTT numbers are only
#     comparable on a host that has not already been hammered for an hour;
#   - t06 real-time UDP runs sixth, right beside t05, for the same reason.
# Everything else is ordered cheap-to-expensive so a broken stack fails early.
# Every test lives here or in RUNBOOK - nowhere else. A test in neither list is dead code that still looks
# maintained (t19 and t20 were exactly that until 2026-09-17).
TESTS="t01 t02 t03 t04 t05 t06 t07 t08 t09 t10 t11 t12 t13 t14 t15 t16 t17 t18 t19 t20 t21 t22 t23 t24"
RUNBOOK="t25 t26 t27 t28 t29 t30 t31 t32"

# --very-fast: every long step cut to the shortest setting that still exercises its mechanism. These are exported as
# per-test knobs, so an explicit T<NN>_<VAR> in the environment still wins.
if [ "$VERY_FAST" = 1 ]; then
  export SUITE_FAST=1
  : "${BYTES:=2000000}" "${CAP:=30}" "${REPS:=1}"; export BYTES CAP REPS
  veryfast() { local v="${1%%=*}"; [ -n "${!v:-}" ] || export "$1"; }
  veryfast T03_N=3
  veryfast T05_PHASE_S=8;  veryfast T05_PARALLEL="1 3"
  veryfast T06_ECHO_DUR=15; veryfast T06_STREAM_DUR=15
  veryfast T09_RUNGS="0 25"; veryfast T09_STREAM_S=8
  veryfast T10_DUR=130; veryfast T10_REPEATS=1; veryfast T10_T_KILL=20   # window after the kill must exceed RECOVER_MAX: a removed peer is noticed only by the liveness ping, ~75 s
  veryfast T11_CALL_S=8; veryfast T11_POLL_N=4
  veryfast T12_UPSTREAMS=16; veryfast T12_PASSES=1
  veryfast T13_MTUS="1420 940"; veryfast T13_STREAM_S=10
  veryfast T15_DELAYS="0 5"
  veryfast T18_LADDER="4 8"; veryfast T18_STEP_S=12
  veryfast T19_TRICKLES=100
  veryfast T20_LOSSES=5; veryfast T20_STEP_S=12
  veryfast T21_DUR=20
  veryfast T22_LADDER="1 2 4"; veryfast T22_CAP=45
  veryfast T23_DUR=60; veryfast T23_INTERVAL=30
  veryfast T24_DUR=25
fi

[ -n "$ONLY" ] && TESTS="${ONLY//,/ }"

# Does this stack have a T03-repeatability-baseline band? Without one, nothing host-dependent is scored.
export SUITE_BANDS_DIR="${SUITE_BANDS_DIR:-${SUITE_OUT_DIR}/bands}"
KEY=$(SUITE_RUN="$SUITE_RUN" bash -c "source '${HERE}/lib.sh' >/dev/null 2>&1; stack_key" 2>/dev/null || echo unknown)
if [ -s "${SUITE_BANDS_DIR}/${KEY}.json" ] || case " $TESTS " in *" t03 "*) true;; *) false;; esac; then
  BAND_NOTE="T03-repeatability-baseline band present for stack ${KEY}"
else
  BAND_NOTE="NO T03-repeatability-baseline BAND for stack ${KEY} - host-dependent gates are RECORDED, not scored. t03 is in the run, so this resolves itself after one full run."
fi
{ echo "run_id=$RUN_ID cell=${SUITE_CELL:-} ref_cell=${SUITE_REF_CELL:-} fast=${VERY_FAST} fast=${SUITE_FAST:-0} started=$(date -u +%FT%TZ)"
  echo "tests=$TESTS"; echo "stack_key=$KEY"; echo "$BAND_NOTE"; } | tee "${SUITE_RUN}/run.txt"
echo "$BAND_NOTE" | grep -q '^NO T03-repeatability-baseline' && echo "!! ${BAND_NOTE}" >&2 || true

fail=0
for t in $TESTS; do
  case ",$SKIP," in *",$t,"*) echo "SKIP $t: --skip"; continue;; esac
  echo "=== $t $(date -u +%T) ==="
  t0=$(date +%s)
  # per-test environment: a variable named T09_STREAM_S=10 is passed to t09 as STREAM_S=10 and to nothing
  # else. Long tests share knob names (DUR, STREAM_S) with short ones, so a global override would break them.
  up=$(echo "$t" | tr 'a-z' 'A-Z'); tenv=()
  while IFS= read -r kv; do
    case "$kv" in "${up}_"*) tenv+=("${kv#${up}_}");; esac
  done < <(env)
  [ ${#tenv[@]} -gt 0 ] && echo "  per-test env: ${tenv[*]}"
  if env "${tenv[@]}" "${HERE}/${t}.sh" 2>&1 | tee -a "${SUITE_RUN}/console.log"; then rc=0; else rc=${PIPESTATUS[0]}; fi
  echo "$(date -u +%T) $t rc=$rc took=$(( $(date +%s) - t0 ))s" | tee -a "${SUITE_RUN}/run.txt"
  [ "$rc" -ne 0 ] && fail=1
  if [ "$t" = t01 ] && [ "$rc" -ne 0 ]; then echo "preconditions failed - aborting profile" | tee -a "${SUITE_RUN}/run.txt"; break; fi
done

python3 - "${SUITE_RUN}" <<'SUM'
import sys, json, csv, collections
run = sys.argv[1]
v = [json.loads(l) for l in open(f"{run}/verdicts.jsonl") if l.strip()]
with open(f"{run}/summary.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["cell", "kind", "test", "status", "msg"])
    [w.writerow([x.get("cell", ""), x.get("kind", ""), x["test"], x["status"], x["msg"]]) for x in v]
by = collections.defaultdict(collections.Counter)
for x in v: by[x.get("kind", "gate")][x["status"]] += 1
for kind in ("gate", "diagnostic", "runbook"):
    if by.get(kind): print(f"{kind:11s}", dict(by[kind]))
g = by.get("gate", {})
print("verdict:", "FAILED" if g.get("FAIL") else "passed", f"({g.get('FAIL',0)} gate failures)",
      "->", f"{run}/summary.csv")
SUM
echo "finished=$(date -u +%FT%TZ) fail=$fail" | tee -a "${SUITE_RUN}/run.txt"
exit $fail
