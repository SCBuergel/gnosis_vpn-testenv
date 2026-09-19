# Repro: the client reconnects every ~85 s, idle, because its liveness ping targets an address the server does not hold

Standalone reproduction. It needs the testenv stack and nothing from `scripts/suite/`. Until 2026-09-19 this directory described the same reconnect cycle as a load defect ("a modest UDP flow makes the client tear the tunnel down"); that reading was wrong, and the idle control below is what shows it.

## What happens

Connect the client to an exit and do nothing. The client's periodic tunnel-liveness ping (`ping 10.128.0.1`, 15 s timeout, sent every 10 s) never gets an answer. After three consecutive timeouts, ~85 s after the session came up, the client logs `tunnel ping exceeded max failures - reconnecting`, removes `wg0_gnosisvpn` and creates a new one about 3.5 s later. Then the same again, for as long as the session lives. Traffic plays no part: the cycle is identical with no traffic and under a 1.5 Mbit/s UDP flow, and the flow itself is delivered at ~97 % between reconnects.

**Cause.** `tunnel_ping_loop` in gnosis_vpn-lib `src/core/runner.rs` (line 283 at `4c0c0e4c`) builds its options as `ping::Options { seq_count: 1, ..Default::default() }`; the default address is 10.128.0.1 (`src/ping.rs`, `impl Default for Options`). It ignores `options.ping_options`, which carries the `[connection.ping] address` from the config and which the connect-time verification (`src/connection/up/runner.rs`, line 195) does use. This testenv's server holds only 10.129.0.1 on `wggvpn`, and its generated `client.toml` correctly says `address = "10.129.0.1"`, so the connect-time ping succeeds and every periodic one times out. Any deployment whose WireGuard gateway is not 10.128.0.1 is affected.

**Fix in the client:** make the periodic loop use `options.ping_options`. **Stopgap in testenv:** `server-start` now adds `SERVER_PING_ALIAS` (10.128.0.1) to the server's `wggvpn`, and T01-topology-preconditions fails a run whose server lacks either address.

## Pinned versions

| component | version | source |
| --- | --- | --- |
| client | `gnosis_vpn-ctl 0.96.3`, image `gnosis_vpn-client:0.96.3-4c0c0e4c` | gnosis_vpn-client `release/hoprdv4` @ `4c0c0e4c`, built with `just build-client-glibc` |
| hoprd + localcluster | `hoprd 4.1.3` | hoprnet/hoprd `release/4.1` @ `60269a3`, `cargo build --release`; localcluster from the same tree |
| server | `gnosis_vpn-server 0.7.0` | release image, `wggvpn` at 10.129.0.1/32 |
| testenv | `5a19eee` | this repo, 3-node localcluster, `CLUSTER_ENV` empty, all component defaults |

Also seen on client 0.96.2 and on hoprd 4.1.2 of the `release/4.1` line.

## Steps

```sh
# 1. stack up and settled (~3 min). To reproduce the bug, start the server WITHOUT the stopgap:
SERVER_PING_ALIAS= just up-nobuild && sleep 180

# 2. connect and idle for 200 s; prints the client's ping/reconnect log lines on one timeline
repro/udp-reconnect/repro.sh

# 3. give the server the address the client pings, and repeat: no timeouts, no reconnects
docker exec gnosis_vpn-server-0 ip addr add 10.128.0.1/32 dev wggvpn
repro/udp-reconnect/repro.sh

# optional: the same with a 1.5 Mbit/s UDP echo flow through the tunnel
repro/udp-reconnect/repro.sh --load
```

## Expected output (2026-09-19, verify-liveness run)

Without the address, idle, no traffic at all:

```
server wggvpn addresses: 10.129.0.1/32    client [connection.ping] address: 10.129.0.1
21:28:56  session up (node-0)
21:28:56  client: starting tunnel ping probe interval=10s
21:29:20  client: ping 400 timed out
21:29:46  client: ping 401 timed out
21:30:10  client: ping 402 timed out
21:30:10  client: tunnel ping exceeded max failures - reconnecting
21:30:10  client: network link removed index=68 name="wg0_gnosisvpn"
21:30:14  client: created TUN device interface=wg0_gnosisvpn
21:30:44 / 21:31:08 / 21:31:32  ping 406-408 timed out -> reconnecting; link 69 removed; created TUN 21:31:36
21:32:02 / 21:32:28  ping 412, 413 timed out                      (third reconnect ~25 s away)
21:32:34  tunnel-ping timeouts: 7   reconnects: 2
```

With 10.128.0.1 added to `wggvpn`, idle:

```
server wggvpn addresses: 10.129.0.1/32 10.128.0.1/32
21:32:46  session up (node-0)
21:36:24  tunnel-ping timeouts: 0   reconnects: 0
```

With the address and a 1.5 Mbit/s echo flow for 200 s:

```
21:36:31  load start: 1.5 Mbit/s, 1200 B datagrams, echo on 198.18.0.2:8901
          load:   summary: 202s, sent 31250, echoed 31250 (0.0% of sent lost), 0 seconds with the tunnel gone
21:40:11  tunnel-ping timeouts: 0   reconnects: 0
```

Signature to grep for in `docker logs gnosis_vpn-client`:

```
TunnelPingResult: Error(Ping timed out)
tunnel ping exceeded max failures - reconnecting
network link removed index=N name="wg0_gnosisvpn"
created TUN device interface=wg0_gnosisvpn
```

## Notes

- A manual `ping 10.128.0.1` from inside the client container gets no reply while `ping 10.129.0.1` answers in ~45 ms; that one command is the whole diagnosis.
- Independence from rate, direction and MTU was the tell that this was not a load defect, and the first ping timeout is logged before any load starts. A defect caused by load has to depend on the load.
- `udp_echo_load.py` uses an unbound UDP socket on purpose: inside the client container the only route to `198.18.0.0/24` is the tunnel, so the flow follows the interface across a reconnect without re-bind logic.
