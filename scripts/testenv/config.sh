#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/common.sh"

: "${CONFIG_DIR:?}"
: "${TEMPLATES_DIR:?}"

# Derive client config and system-test artifacts from live cluster status.
gen() {
    : "${DATA_DIR:?}"
    : "${LOCALCLUSTER_BIN:?}"
    : "${HOPS:?}"
    mkdir -p "${CONFIG_DIR}"

    local status blokli_url
    status=$("${LOCALCLUSTER_BIN}" status --data-dir "${DATA_DIR}")
    blokli_url=$(echo "${status}" | jq -r '.blokli_url')

    # One [destinations.node-N] block per cluster exit node
    local destinations="" node id address block
    while IFS= read -r node; do
        id=$(echo "${node}" | jq -r '.id')
        address=$(echo "${node}" | jq -r '.address')
        block=$(DEST_ID="${id}" DEST_ADDRESS="${address}" DEST_HOPS="${HOPS}" \
            envsubst "\$DEST_ID,\$DEST_ADDRESS,\$DEST_HOPS" \
            <"${TEMPLATES_DIR}/destination.toml.tpl")
        destinations+="${block}"$'\n'
        # Optional 0-hop twin (node-N-h0) for the hopcount A/B test; needs --allow-insecure on the client.
        if [ "${HOPS0_ALSO:-0}" = "1" ]; then
            block=$(DEST_ID="${id}-h0" DEST_ADDRESS="${address}" DEST_HOPS="0" \
                envsubst "\$DEST_ID,\$DEST_ADDRESS,\$DEST_HOPS" \
                <"${TEMPLATES_DIR}/destination.toml.tpl")
            destinations+="${block}"$'\n'
        fi
    done < <(echo "${status}" | jq -c '.nodes[]')

    # The PIX block has to agree with how the cluster was started, so it comes off the same switch.
    local pix_section
    if [ -n "${CLUSTER_ENABLE_PIX:-}" ]; then
        pix_section=$(cat "${TEMPLATES_DIR}/pix-on.toml.tpl")
    else
        pix_section=$(cat "${TEMPLATES_DIR}/pix-off.toml.tpl")
    fi

    DESTINATIONS="${destinations}" PIX_SECTION="${pix_section}" \
        envsubst "\$DESTINATIONS,\$PIX_SECTION" \
        <"${TEMPLATES_DIR}/client.toml.tpl" \
        >"${CONFIG_DIR}/client.toml"

    echo "${blokli_url}" >"${CONFIG_DIR}/blokli_url"
    echo "Generated ${CONFIG_DIR}/client.toml"

    # Persist the extra identity artifacts needed by client and system tests.
    # extra_id_<i>.* is every extra (extra 0 is the primary client, extra 1 the second client for
    # T22-concurrent-clients/T19-background-load/T21-passive-observer); extra_id.* mirrors extra 0.
    local extra keystore_path i ext
    echo "${status}" | jq -c '.extras[]' | while IFS= read -r extra; do
        i=$(echo "${extra}" | jq -r '.id')
        keystore_path=$(echo "${extra}" | jq -r '.keystore_path')
        cp "${keystore_path}" "${CONFIG_DIR}/extra_id_${i}.id"
        echo "${extra}" | jq -r '.password' >"${CONFIG_DIR}/extra_id_${i}.password"
        echo "${extra}" | jq -r '.safe_address' >"${CONFIG_DIR}/extra_id_${i}.safe"
        echo "${extra}" | jq -r '.module_address' >"${CONFIG_DIR}/extra_id_${i}.module"
        if [ "${i}" = "0" ]; then
            for ext in id password safe module; do cp "${CONFIG_DIR}/extra_id_0.${ext}" "${CONFIG_DIR}/extra_id.${ext}"; done
        fi
        echo "Saved extra identity ${i} artifacts to ${CONFIG_DIR}"
    done
}

# Rewrite the generated config to point a client on another machine at this host's LAN IP.
gen_on_network() {
    : "${NETWORK_BUNDLE_DIR:?}"
    local ip
    ip=$(lan_ip)
    sed "s/127\.0\.0\.1/${ip}/g" "${CONFIG_DIR}/client.toml" >"${CONFIG_DIR}/client-on-network.toml"
    sed "s/localhost/${ip}/" "${CONFIG_DIR}/blokli_url" >"${CONFIG_DIR}/blokli_url-on-network"
    mkdir -p "${NETWORK_BUNDLE_DIR}"
    cp "${CONFIG_DIR}/client-on-network.toml" \
        "${CONFIG_DIR}/extra_id.id" \
        "${CONFIG_DIR}/extra_id.password" \
        "${NETWORK_BUNDLE_DIR}/"
    echo "Generated ${CONFIG_DIR}/client-on-network.toml (exit server via ${ip})"
    echo "Bundled remote-client files into ${NETWORK_BUNDLE_DIR}"
}

case "${1:-}" in
gen) gen ;;
gen-on-network) gen_on_network ;;
*) die "usage: config.sh gen|gen-on-network" ;;
esac
