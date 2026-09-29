# shellcheck shell=bash
# Helpers shared by the scripts/testenv dispatchers. Sourced, never executed.

die() {
    printf '%s\n' "$@" >&2
    exit 1
}

require_localcluster_bin() {
    : "${LOCALCLUSTER_BIN:?}"
    [ -f "${LOCALCLUSTER_BIN}" ] || die \
        "Error: hoprd-localcluster not found at ${LOCALCLUSTER_BIN}" \
        "Run 'just build-cluster' to build it first"
}

cluster_status_json() {
    : "${DATA_DIR:?}"
    "${LOCALCLUSTER_BIN}" status --data-dir "${DATA_DIR}" 2>/dev/null
}

# A LAN IP is spliced into host:port strings and firewall rules, so a bad one must fail here.
is_ipv4() {
    local octets octet
    IFS=. read -r -a octets <<<"$1"
    [ "${#octets[@]}" -eq 4 ] || return 1
    for octet in "${octets[@]}"; do
        [[ ${octet} =~ ^[0-9]{1,3}$ ]] || return 1
        [ "${octet}" -le 255 ] || return 1
    done
}

# Resolve the LAN-reachable IP: LAN_IP override, or auto-detect via default route.
lan_ip() {
    if [ -n "${LAN_IP:-}" ]; then
        is_ipv4 "${LAN_IP}" ||
            die "Error: LAN_IP='${LAN_IP}' is not an IPv4 dotted-quad (hostnames, IPv6 and out-of-range octets aren't supported)"
        echo "${LAN_IP}"
        return 0
    fi
    local detected
    detected=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p')
    [ -n "${detected}" ] ||
        die "Error: could not auto-detect a LAN IP (no default route?) — set LAN_IP explicitly"
    echo "${detected}"
}

# Bring the Curvy stack's HOPRD_CURVY_* overrides into the environment (the file exports them).
load_curvy_env() {
    : "${CURVY_STACK_ENV:?}"
    [ -f "${CURVY_STACK_ENV}" ] || die "Error: no Curvy stack — run 'just curvy-stack-up' first"
    # shellcheck source=/dev/null
    . "${CURVY_STACK_ENV}"
}
