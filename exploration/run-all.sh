#!/usr/bin/env bash
# run-all.sh — run the exploration battery serially, one scenario at a time (they must not share the stack).
# Detached-friendly: launch under systemd-run/nohup. Each scenario writes its own run dir under exploration/runs.
set -uo pipefail
cd "$(dirname "$0")/.."
source /root/testenv/env.sh
export CLIENT_IMAGE=gnosis_vpn-client:0.96.3-4c0c0e4c
export HOPRD_BIN=/root/testenv/hoprd-4arb/target/release/hoprd
export LOCALCLUSTER_BIN=/root/testenv/hoprd-4arb/target/release/hoprd-localcluster
SUMMARY="exploration/runs/BATTERY-$(date -u +%Y%m%dT%H%M%SZ).tsv"
: > "$SUMMARY"
# push pure-download to and past the single-host ceiling (~10-13 Mbit/s) to test overload->break vs loss
export RATES="${RATES:-6 8 10 12 15}"
SCENS="${SCENS:-pure-download reorder mtu-frag churn burst-idle bidir idle-resume soak}"
for s in $SCENS; do
  echo "===== $(date -u +%T) scenario: $s ====="
  bash exploration/explore.sh "$s" 2>&1
  # append this scenario's results to the battery summary
  last=$(ls -td exploration/runs/*-"$s" 2>/dev/null | head -1)
  [ -n "$last" ] && [ -f "$last/results.tsv" ] && cat "$last/results.tsv" >> "$SUMMARY"
  echo
done
echo "===== battery done $(date -u +%T) ====="
echo "== SUMMARY =="; cat "$SUMMARY"
