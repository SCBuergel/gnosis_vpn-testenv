#!/usr/bin/env bats
# Offline tests for scripts/testenv/config.sh: envsubst rendering of templates/ against a fake
# hoprd-localcluster status - no cluster, no Docker.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../testenv/config.sh"
    export LOCALCLUSTER_BIN="${BATS_TEST_DIRNAME}/fakes/hoprd-localcluster"
    export TEMPLATES_DIR="${BATS_TEST_DIRNAME}/../../templates"
    export DATA_DIR="${BATS_TEST_TMPDIR}/data"
    export CONFIG_DIR="${BATS_TEST_TMPDIR}/config"
    export HOPS=1
}

# A status fixture carrying the extra identity gen-config persists for the client/system tests.
status_with_extra() {
    local keystore="${BATS_TEST_TMPDIR}/extra.id"
    echo "keystore-bytes" >"$keystore"
    cat <<JSON
{ "state": "running", "blokli_url": "http://localhost:8080",
  "nodes": [ { "id": "0", "address": "0xAAA", "p2p": "172.30.0.1:9000" } ],
  "extras": [ { "id": "0", "keystore_path": "${keystore}", "password": "pw", "safe_address": "0xSAFE", "module_address": "0xMOD" } ] }
JSON
}

@test "gen writes one destination block per cluster node" {
    run "$SCRIPT" gen
    [ "$status" -eq 0 ]
    grep -q '^\[destinations.node-0\]$' "${CONFIG_DIR}/client.toml"
    grep -q '^\[destinations.node-1\]$' "${CONFIG_DIR}/client.toml"
    grep -q '^address = "0xAAA"$' "${CONFIG_DIR}/client.toml"
    grep -q '^address = "0xBBB"$' "${CONFIG_DIR}/client.toml"
}

@test "gen records the cluster's blokli url" {
    run "$SCRIPT" gen
    [ "$status" -eq 0 ]
    [ "$(cat "${CONFIG_DIR}/blokli_url")" = "http://localhost:8080" ]
}

@test "gen leaves no unexpanded placeholders behind" {
    run "$SCRIPT" gen
    [ "$status" -eq 0 ]
    run grep -c '\${' "${CONFIG_DIR}/client.toml"
    [ "$output" = "0" ]
}

@test "CLUSTER_ENABLE_PIX unset selects the pix-off block" {
    run "$SCRIPT" gen
    [ "$status" -eq 0 ]
    grep -q 'enabled = false' "${CONFIG_DIR}/client.toml"
    ! grep -q '\[pix_strategy\]' "${CONFIG_DIR}/client.toml"
}

@test "CLUSTER_ENABLE_PIX set selects the pix-on block" {
    CLUSTER_ENABLE_PIX=1 run "$SCRIPT" gen
    [ "$status" -eq 0 ]
    grep -q 'enabled = true' "${CONFIG_DIR}/client.toml"
    grep -q '^\[pix_strategy\]$' "${CONFIG_DIR}/client.toml"
}

@test "gen copies the extra identity artifacts when the cluster reports one" {
    FAKE_LC_STATUS="$(status_with_extra)"
    export FAKE_LC_STATUS
    run "$SCRIPT" gen
    [ "$status" -eq 0 ]
    [ "$(cat "${CONFIG_DIR}/extra_id.id")" = "keystore-bytes" ]
    [ "$(cat "${CONFIG_DIR}/extra_id.password")" = "pw" ]
    [ "$(cat "${CONFIG_DIR}/extra_id.safe")" = "0xSAFE" ]
    [ "$(cat "${CONFIG_DIR}/extra_id.module")" = "0xMOD" ]
}

@test "gen-on-network rewrites loopback targets to the LAN IP and bundles the client files" {
    export LAN_IP=192.168.5.9
    export NETWORK_BUNDLE_DIR="${CONFIG_DIR}/on-network"
    FAKE_LC_STATUS="$(status_with_extra)"
    export FAKE_LC_STATUS
    run "$SCRIPT" gen
    [ "$status" -eq 0 ]
    run "$SCRIPT" gen-on-network
    [ "$status" -eq 0 ]
    grep -q 'target = "192.168.5.9:8000"' "${CONFIG_DIR}/client-on-network.toml"
    grep -q 'target = "192.168.5.9:51821"' "${CONFIG_DIR}/client-on-network.toml"
    [ "$(cat "${CONFIG_DIR}/blokli_url-on-network")" = "http://192.168.5.9:8080" ]
    [ -f "${NETWORK_BUNDLE_DIR}/client-on-network.toml" ]
}
