#!/usr/bin/env bash
# Mirrors how system-tests owns its daemon instead of attaching to a pre-running one. Needs sudo
# (WireGuard + routing table) and a pre-existing worker user — macOS has no useradd, so unlike the
# Linux-only system-tests runner this does not create one. Full tunnel: while it runs, this
# machine's traffic egresses through the exit under test.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/common.sh"

: "${REPO_DIR:?}"
: "${E2E_IMAGE:?}"
: "${E2E_OUT_DIR:?}"
: "${E2E_ROOT_BINARY:?E2E_ROOT_BINARY must point at a gnosis_vpn-root binary for this host}"
: "${E2E_WORKER_BINARY:?E2E_WORKER_BINARY must point at a gnosis_vpn-worker binary for this host}"
: "${E2E_CONFIG:?E2E_CONFIG must point at a client config (e.g. rotsee.toml)}"
: "${E2E_IDENTITY_FILE:?E2E_IDENTITY_FILE must point at the HOPR identity file}"
: "${E2E_IDENTITY_PASS_FILE:?E2E_IDENTITY_PASS_FILE must point at a file holding the identity password}"
: "${E2E_BLOKLI_URL:?E2E_BLOKLI_URL must be set (e.g. https://blokli.rotsee.hoprnet.link)}"

# gnosis_vpn-worker runs unprivileged, so its binary/identity/config must be readable by
# E2E_WORKER_USER — a path under someone's home usually is not. Stage into a world-readable dir.
stage="${E2E_STAGE_DIR:-/tmp/gnosis_vpn-e2e-stage}"
rm -rf "${stage}"
mkdir -p "${stage}"
cp "${E2E_ROOT_BINARY}" "${stage}/gnosis_vpn-root"
cp "${E2E_WORKER_BINARY}" "${stage}/gnosis_vpn-worker"
cp "${E2E_CONFIG}" "${stage}/config.toml"
cp "${E2E_IDENTITY_FILE}" "${stage}/identity.id"
# Default to the .safe sitting next to the identity, as gnosis_vpn lays it out
safe_file="${E2E_IDENTITY_SAFE_FILE:-${E2E_IDENTITY_FILE%.id}.safe}"
if [ -f "${safe_file}" ]; then
    cp "${safe_file}" "${stage}/identity.safe"
else
    echo "WARNING: no .safe found next to the identity (${safe_file}); the client will try to onboard a NEW safe" >&2
fi
chmod -R a+rX "${stage}"
chmod a+rx "${stage}/gnosis_vpn-root" "${stage}/gnosis_vpn-worker"

CLIENT_MODE=spawn \
    WORKER_USER="${E2E_WORKER_USER:-gnosisvpn-dev}" \
    GVPN_ROOT_BIN="${stage}/gnosis_vpn-root" \
    GVPN_WORKER_BIN="${stage}/gnosis_vpn-worker" \
    GVPN_CONFIG="${stage}/config.toml" \
    GVPN_IDENTITY_FILE="${stage}/identity.id" \
    GVPN_IDENTITY_PASS="$(cat "${E2E_IDENTITY_PASS_FILE}")" \
    GVPN_IDENTITY_SAFE="$([ -f "${stage}/identity.safe" ] && echo "${stage}/identity.safe" || echo "")" \
    GVPN_BLOKLI_URL="${E2E_BLOKLI_URL}" \
    PING_TARGETS="${PING_TARGETS:-1.1.1.1,10.128.0.1}" \
    "${REPO_DIR}/e2e/run.sh" "$@"
