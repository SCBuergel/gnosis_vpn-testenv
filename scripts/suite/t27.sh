#!/usr/bin/env bash
# T27-role-split — Role split: exit version vs relay version. Blocked: hoprd-localcluster runs one --hoprd-bin for every node
# (catalogue extension 1). Records SKIP with the reason so the profile output shows the gap.
source "$(dirname "$0")/lib.sh"
suite_kind runbook
[ "${1:-}" = "--help" ] && { sed -n '2,3p' "$0"; usage_common; exit 0; }
suite_init; verdict T27-role-split SKIP "needs per-role hoprd binaries in hoprd-localcluster (extension 1)"; exit 0
