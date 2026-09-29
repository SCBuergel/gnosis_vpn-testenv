#!/usr/bin/env bats
# Offline tests for scripts/testenv/common.sh: the LAN IP resolver, sourced directly - no network.

setup() {
    COMMON="${BATS_TEST_DIRNAME}/../testenv/common.sh"
}

lan_ip_with_fake_ip_route() {
    local route_output="$1"
    cat >"${BATS_TEST_TMPDIR}/ip" <<EOF
#!/usr/bin/env bash
printf '%s\n' "${route_output}"
EOF
    chmod +x "${BATS_TEST_TMPDIR}/ip"
    PATH="${BATS_TEST_TMPDIR}:${PATH}" bash -c "source '${COMMON}'; lan_ip"
}

@test "LAN_IP override is used verbatim" {
    run bash -c "source '${COMMON}'; LAN_IP=10.1.2.3 lan_ip"
    [ "$status" -eq 0 ]
    [ "$output" = "10.1.2.3" ]
}

@test "a hostname as LAN_IP is rejected" {
    run bash -c "source '${COMMON}'; LAN_IP=my-host.local lan_ip"
    [ "$status" -eq 1 ]
    [[ "$output" == *"is not an IPv4 dotted-quad"* ]]
}

@test "an IPv6 address as LAN_IP is rejected" {
    run bash -c "source '${COMMON}'; LAN_IP=fe80::1 lan_ip"
    [ "$status" -eq 1 ]
    [[ "$output" == *"is not an IPv4 dotted-quad"* ]]
}

@test "an out-of-range octet in LAN_IP is rejected" {
    run bash -c "source '${COMMON}'; LAN_IP=999.999.999.999 lan_ip"
    [ "$status" -eq 1 ]
    [[ "$output" == *"is not an IPv4 dotted-quad"* ]]
}

@test "without LAN_IP the default route's src address is used" {
    run lan_ip_with_fake_ip_route "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.42 uid 1000"
    [ "$status" -eq 0 ]
    [ "$output" = "192.168.1.42" ]
}

@test "no default route is an error naming LAN_IP" {
    run lan_ip_with_fake_ip_route ""
    [ "$status" -eq 1 ]
    [[ "$output" == *"could not auto-detect a LAN IP"* ]]
}
