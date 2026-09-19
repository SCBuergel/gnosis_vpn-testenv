#!/usr/bin/env bash
# T28-transport-ab — Application-transport A/B (HTTP/1.1 vs HTTP/3, CUBIC vs BBR at the app layer). Documented negative; needs an
# HTTP/3 target and a curl with h3. SKIPs unless the client's curl reports HTTP3 and TARGET_H3_PORT is set.
source "$(dirname "$0")/lib.sh"
suite_kind runbook
[ "${1:-}" = "--help" ] && { sed -n '2,3p' "$0"; usage_common; exit 0; }
suite_init
in_client 'curl --version' | grep -q HTTP3 && [ -n "${TARGET_H3_PORT:-}" ] || { verdict T28-transport-ab SKIP "no HTTP/3-capable curl in the client image / no TARGET_H3_PORT (documented negative, see catalogue)"; exit 0; }
verdict T28-transport-ab SKIP "h3 arm not implemented in this revision"; exit 0
