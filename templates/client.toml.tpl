version = 6

${DESTINATIONS}
# the local cluster announces private/local IPs (Docker network, or loopback in
# host-native mode), which the client filters out by default
[connection]
probe_local_addresses = true

[connection.bridge]
target = "127.0.0.1:8000"

[connection.wg]
target = "127.0.0.1:51821"

# WireGuard server interface address — defined in gnosis_vpn-server/docker/wggvpn.conf
[connection.ping]
address = "10.129.0.1"

# wg-quick's DNS step shells out to resolvconf, which can't manage Docker's
# auto-generated /etc/resolv.conf and aborts the whole `up` — skip it entirely
[wireguard]
dns = { overwrite = false }

${PIX_SECTION}
