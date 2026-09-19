#!/usr/bin/env bash
#
# matrix.sh - run a profile across version/config cells (catalogue T25-knob-ab/T26-version-matrix).
#
#   matrix.sh CELLS_FILE PROFILE [--fast] [--reverse] [--only ...]
#
# CELLS_FILE: one cell per line, "name|CLIENT_IMAGE|HOPRD_BIN|CLUSTER_ENV|CLIENT_EXTRA_ENV|LOCALCLUSTER_BIN" (empty fields allowed,
# '#' comments). For every cell: just down, bring the stack up with the cell's settings (up-nobuild), run the profile
# with SUITE_CELL=name, then just down. Pass --reverse to run the cells in reverse order (second pass, R2/T26-version-matrix).
# Results: SUITE_OUT_DIR/<stamp>-<cell>/ per cell and SUITE_OUT_DIR/matrix-<stamp>.csv across cells.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; TESTENV_DIR="$(cd "${HERE}/../.." && pwd)"
CELLS="${1:?cells file}"; PROFILE="${2:-regression}"; shift 2 || true
REV=0; EXTRA=()
while [ $# -gt 0 ]; do case "$1" in --reverse) REV=1;; --help|-h) sed -n '3,11p' "$0"; exit 0;; *) EXTRA+=("$1");; esac; shift; done
: "${SUITE_OUT_DIR:=/tmp/gnosis_vpn-testenv-suite}"; STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mapfile -t LINES < <(grep -vE '^\s*(#|$)' "$CELLS"); [ "$REV" = 1 ] && mapfile -t LINES < <(printf '%s\n' "${LINES[@]}" | tac)
fail=0
for line in "${LINES[@]}"; do
  IFS='|' read -r name cimg hbin cenv clenv lcbin <<< "$line"; lcbin="${lcbin:-${LOCALCLUSTER_BIN:-}}"
  echo "##### cell $name: client=$cimg hoprd=$hbin cluster_env='$cenv' client_env='$clenv' localcluster=$lcbin ($(date -u +%T))"
  ( cd "$TESTENV_DIR" && just down >/dev/null 2>&1 ) || true
  if ! ( cd "$TESTENV_DIR" && CLIENT_IMAGE="$cimg" HOPRD_BIN="$hbin" LOCALCLUSTER_BIN="$lcbin" CLUSTER_ENV="$cenv" CLIENT_EXTRA_ENV="$clenv" just up-nobuild ); then
    echo "cell $name: stack failed to come up" | tee -a "${SUITE_OUT_DIR}/matrix-${STAMP}.log"; fail=1; continue
  fi
  if [ "${EXTRA_IDENTITIES:-1}" -ge 2 ]; then ( cd "$TESTENV_DIR" && CLIENT_IMAGE="$cimg" CLIENT_EXTRA_ENV="$clenv" just client2-start ) || true; fi
  # a fresh client syncs and health-checks before any destination is Ready; give it up to READY_TIMEOUT s
  dest="${DEST:-node-0}"; waited=0
  until docker exec gnosis_vpn-client gnosis_vpn-ctl status 2>/dev/null | grep -q "^${dest} Route health: Ready"; do
    sleep 10; waited=$((waited+10)); [ "$waited" -ge "${READY_TIMEOUT:-600}" ] && { echo "cell $name: $dest not Ready after ${waited}s"; break; }
  done
  echo "cell $name: $dest Ready after ${waited}s"
  ( cd "$TESTENV_DIR" && SUITE_CELL="$name" CLIENT_IMAGE="$cimg" HOPRD_BIN="$hbin" LOCALCLUSTER_BIN="$lcbin" CLUSTER_ENV="$cenv" CLIENT_EXTRA_ENV="$clenv" just suite "$PROFILE" --run-id "${STAMP}-${name}" "${EXTRA[@]}" ) || fail=1
  ( cd "$TESTENV_DIR" && LOCALCLUSTER_BIN="$lcbin" HOPRD_BIN="$hbin" just down >/dev/null 2>&1 ) || true
done
python3 - "${SUITE_OUT_DIR}" "${STAMP}" <<'PY'
import sys,json,glob,csv,os
out,stamp=sys.argv[1],sys.argv[2]; rows=[]
for d in sorted(glob.glob(f"{out}/{stamp}-*")):
    try:
        for l in open(f"{d}/verdicts.jsonl"):
            if l.strip(): v=json.loads(l); rows.append([v.get("cell"),v["test"],v["status"],v["msg"]])
    except FileNotFoundError: pass
with open(f"{out}/matrix-{stamp}.csv","w",newline="") as f:
    w=csv.writer(f); w.writerow(["cell","test","status","msg"]); w.writerows(rows)
print(f"matrix summary: {out}/matrix-{stamp}.csv ({len(rows)} verdicts)")
PY
exit $fail
