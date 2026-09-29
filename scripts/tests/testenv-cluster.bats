#!/usr/bin/env bats
# Offline tests for scripts/testenv/cluster.sh: status-JSON and node-config parsing against a fake
# hoprd-localcluster - no cluster, no Docker.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../testenv/cluster.sh"
    export LOCALCLUSTER_BIN="${BATS_TEST_DIRNAME}/fakes/hoprd-localcluster"
    export DATA_DIR="${BATS_TEST_TMPDIR}/data"
    mkdir -p "$DATA_DIR"
}

write_node_config() {
    cat >"${DATA_DIR}/hoprd_cfg_0.yaml"
}

@test "p2p-host strips the port off the first node's dial address" {
    run "$SCRIPT" p2p-host
    [ "$status" -eq 0 ]
    [ "$output" = "172.30.0.1" ]
}

@test "p2p-host is empty when no cluster is running" {
    FAKE_LC_STATUS='{"state":"not_running","nodes":[]}' run "$SCRIPT" p2p-host
    [ "$status" -eq 0 ]
    [ "$output" = "" ]
}

@test "has-pix is yes when the node config carries a Pix strategy" {
    write_node_config <<'YAML'
strategy:
  strategies:
    - Pix:
        price_per_byte: "0.0001 wxHOPR"
YAML
    run "$SCRIPT" has-pix
    [ "$status" -eq 0 ]
    [ "$output" = "yes" ]
}

@test "has-pix is no when the node config has other strategies" {
    write_node_config <<'YAML'
strategy:
  strategies:
    - AutoRedeeming:
        redeem_only_aggregated: true
YAML
    run "$SCRIPT" has-pix
    [ "$status" -eq 0 ]
    [ "$output" = "no" ]
}

@test "has-pix is no when no node config exists yet" {
    run "$SCRIPT" has-pix
    [ "$status" -eq 0 ]
    [ "$output" = "no" ]
}

@test "an unknown subcommand prints usage and fails" {
    run "$SCRIPT" bogus
    [ "$status" -eq 1 ]
    [[ "$output" == *"usage: cluster.sh"* ]]
}
