# Gnosis VPN test environment

Local development and system-test stack for Gnosis VPN. Orchestrates a HOPR
localcluster, one or more containerised Gnosis VPN server (exit node) instances,
and a containerised Gnosis VPN client against them.

## Prerequisites

- [Nix](https://nixos.org/) with flakes enabled
- [just](https://just.systems/) (system-level; must be available outside any Nix
  shell to avoid nesting issues)
- Docker (or Podman / Apple `container`)
- Sibling repos checked out at the paths below (overridable)

## Why the client runs in its own container

The client and the exit-node servers used to run on the same host network. The
client's full-tunnel WireGuard route (`0.0.0.0/1` + `128.0.0.0/1` via `wg0`) is
installed into the _main routing table_, so once it was up, it captured every
outbound packet on that host — including the exit-node container's own
Docker-NATed egress to the real internet. That packet would get pulled back into
the tunnel instead of leaving, producing a routing loop rather than internet
access.

Running the client in its own container fixes this architecturally: its
full-tunnel route now lives only in the client container's own network
namespace, so it can no longer capture the exit-node container's (or the host's)
traffic. The localcluster still runs natively on the host; the client container
reaches it over a dedicated Docker network (see below).

## Sibling repo paths

By default the recipes expect these repos next to each other:

```
parent/
  gnosis_vpn-testenv/   ← this repo
  hoprd/
  gnosis_vpn-server/
  gnosis_vpn-client/
```

Override any path via environment variable — useful in CI where repos are
checked out independently:

```sh
HOPRD_DIR=/ci/hoprd \
GVPN_SERVER_DIR=/ci/gnosis_vpn-server \
GVPN_CLIENT_DIR=/ci/gnosis_vpn-client \
  just up
```

## Development setup

```sh
# 1. Build all components and start the full stack, including the client container
just development-setup

# 2. Tail the client container's logs
docker logs -f gnosis_vpn-client

# 3. Tear everything down (stops client, servers, cluster, and metrics)
just down
```

`just development-setup` builds all components, then brings the whole stack up
(localcluster, VPN server(s), metrics, generated config, and the client
container); the client container gets its WireGuard/routing privileges from
`--cap-add=NET_ADMIN` instead of host root, so no `sudo` is needed for that.

`just up` (and thus `development-setup`) finishes by printing a summary: the
`gnosis_vpn-ctl` commands to control the client and each component's checked-out
commit (and tag, if any) for `gnosis_vpn-client`/`gnosis_vpn-server`/`hoprd`.
Re-print it anytime with `just summary`.

`just up` does the same without the build step; useful for scripting and CI when
components are pre-built.

## Issuing client commands

Once the stack is up, control the running client via `gnosis_vpn-ctl` inside its
container — the client image symlinks the binary onto `PATH` for exactly this
purpose:

```sh
docker exec -it gnosis_vpn-client gnosis_vpn-ctl status
docker exec -it gnosis_vpn-client gnosis_vpn-ctl connect <destination-id>
docker exec -it gnosis_vpn-client gnosis_vpn-ctl --help
```

`gnosis_vpn-ctl` talks to the client's `gnosis_vpn-root` process over
`/var/run/gnosisvpn.sock` inside the container, so no `--socket-path` flag is
needed when run this way.

## Running system tests

```sh
just up             # build + cluster + servers + metrics + gen-config + client
just client-stop    # the runner brings up its own client on the same identity
just system-tests   # runs gnosis_vpn-client's system-test binary against the generated artifacts
just down
```

`just system-tests` runs the client's `gnosis_vpn-system_tests` binary directly
rather than delegating to gnosis_vpn-client's own `system-tests` recipe: that
recipe targets a _deployed_ network, taking its config and Blokli URL from a
checked-in `gnosis_vpn-system_tests/networks/<name>/` fixture picked by
`SYSTEM_TEST_NETWORK`. A localcluster has no such fixture — destinations,
identity and Blokli URL are generated fresh by `gen-config` on every run — so
this repo stages those artifacts and the worker-user setup itself.

It needs `sudo`: the runner creates the worker user if missing, spawns
`gnosis_vpn-root` as root, and brings up a full-tunnel WireGuard interface.
**While it runs, this machine's traffic egresses through the exit under test.**
It also refuses to start while the `gnosis_vpn-client` container is up, since
the runner's own client uses the same HOPR identity (`extra_id.id`) the
container was handed — two nodes on one chain key announce the same peer twice
and fight over the Safe.

`SYSTEM_TEST_STATE_DIR` is wiped at the start of every run: `cluster-stop`
deletes `DATA_DIR` and the chain container, so a state home kept from an earlier
run caches a Safe address that no longer exists on the new chain.

Arguments are passed through to the runner, so the optional download phase is
reachable:

```sh
just system-tests download --attempts 2
```

### Keeping hoprd and gnosis_vpn-client compatible

The two have to be built against the **same `hopr-lib` commit**, and it is not
enough for them to merely both be on `main`. `hoprd` pins it by rev;
`gnosis_vpn-client` reaches it transitively through `edgli`, which tracks
`branch = "master"`, so there the commit lives in `Cargo.lock` and moves on any
`cargo update`. The wire format changes between commits without a protocol
version bump: hoprnet `7c7e0ed8` ("generation-tagged SURB consumption"), for
instance, grew the SURB from 401 to 402 bytes, so a client and a node on either
side of it cannot read each other's replies at all. What that looks like:

- `hoprd` node logs:
  `error while dispatching packet in the session manager error=invalid start protocol version`
- client logs:
  `hopr_transport_probe::probe: cannot deserialize message … Message.version`
- `gnosis_vpn-ctl status`: every destination stuck at `Needs channel`, no
  channels ever opened

Check the pairing before blaming the stack — the two commands must print the
same rev:

```sh
grep -m1 -o 'rev = "[0-9a-f]*"' ../hoprd/Cargo.toml
grep -o 'hoprnet?[^"]*#[0-9a-f]*' ../gnosis_vpn-client/Cargo.lock | sort -u
```

The second command has to print exactly **one** line, and its commit has to be
the rev the first one prints. The client's side is two entries that must agree
with each other as well — `edgli` (which pins `hopr-lib` itself) and
`hopr-utils-session` — and they must agree in _form_, not just commit: Cargo
keys a git source on the reference, so `branch = "master"` in one and
`rev = "<sha>"` in the other are two sources even at the same commit. Either
kind of disagreement builds hoprnet twice, and the duplicate types fail to unify
(`expected hopr_lib::HoprSessionClientConfig, found HoprSessionClientConfig`)
long before anything reaches the wire.

## Running the PIX system test

```sh
just up-pix            # like `up`, but PIX enabled on the cluster *and* in the client config
just system-test-pix   # drive a full deposit → recover → sweep cycle and assert the exit's income
just down
```

PIX pays an exit per byte it delivers back to the client: the exit asks the
client to commit to an SSA, the client deposits `price_per_byte × quota` to a
stealth address, downstream packets carry that SSA's shares home on spent SURBs
until the exit can reconstruct the stealth key, and the exit sweeps the deposit
into its Safe. One cycle covers exactly `quota` bytes of exit → client data, so
the exit's income and the traffic it served are the same number from two sides —
which is what [`pix/run.sh`](pix/run.sh) asserts, entirely from the exit's own
`/metrics` and REST API plus `gnosis_vpn-ctl`. It drives the _containerised_
client, so unlike `system-tests` it needs no `sudo` and touches no host routing.

Both ends have to agree or nothing settles, which is why one switch
(`CLUSTER_ENABLE_PIX`, set by `up-pix`) drives both the cluster's `--enable-pix`
and which PIX block `gen-config` emits:

- **dimensions.** The per-SSA quota is
  `num_ssa_parts × (ssa_part_size + additional_shares) × 1038`, and hopr-lib's
  defaults put it at ~649 MiB — one cycle would need that much downstream
  traffic. The cluster's demo geometry is `8 × (2+2) × 1038` = 33 216 B and
  completes in seconds. Its exit also accepts only quotas in `0 … 1 MiB`, so a
  mismatched client is refused outright with `UnacceptablePixParams`. Matching
  it needs `[connection.pix.dimensions]`, which is why this test requires a
  `gnosis_vpn-client` carrying that config key.
- **price.** `price_per_byte` is _not_ negotiated: each side multiplies the
  agreed quota by its own configured price, so a mismatch leaves the exit
  waiting for a deposit that will never arrive while the client believes it has
  paid.

The run takes roughly five minutes on top of `up-pix`, most of it a 180 s
traffic window. Cycles are paced by the SSA exchange rather than by bytes — each
waits on a deposit landing on chain and on enough of its shares riding home — so
what buys more of them is a longer window, `--seconds N`, not more traffic.

One assertion is deliberately an inequality: `--enable-pix` leaves
auto-redeeming on and there is no flag to disable it, so winning tickets also
credit the exit's Safe and its growth can only be required to be _at least_ the
PIX income. The exactness is carried instead by the integer PIX counters and by
`hopr_strategy_pix_last_sweep_hopr`, which is the wxHOPR of a single sweep.

### Against the Curvy pool

```sh
just up-curvy          # up-pix, but settling through a local Curvy deployment
just system-test-pix   # the same test; it reads the pool off the client image
just down              # also removes the Curvy stack
```

`CLUSTER_PIX_POOL=curvy` (set by `up-curvy`) swaps both ends to the anonymous
Baby JubJub pool: hoprd's `binary-hoprd-pix-curvy`, and the client's
`docker-build-pix-curvy` image. The two have to change together, for the same
curve reason as above.

The Curvy deployment — chain with Blokli, relayer, indexer, batch prover and
gateway, pinned by hoprd's `localcluster/curvy/release.json` — comes up first,
through hoprd's `curvy-localcluster.sh --stack-only`. It is published on the
Docker bridge's gateway so the host-native nodes and the client container reach
it at one address; its gateway moves to `CURVY_GATEWAY_PORT` (3900), off
node-0's API port. The cluster then runs on that chain instead of its own, and
the stack's environment (`$CONFIG_DIR/curvy-stack.env`) reaches the nodes and
the client container as the pool's `HOPRD_CURVY_*` overrides; the client also
gets the proving keys mounted. The first `curvy-stack-up` pulls the release
images and downloads the proving files, which takes a few minutes.

What changes in the test is where the money moves:

- **The client's Safe pays once.** Its first deposit shields a float (100 wxHOPR
  by default) into the Curvy vault, and every later deposit is a private note
  allocated out of it. Nothing on chain ties the client's Safe to the exit's
  income — that is the pool's point — so the client's Safe is reported, not
  asserted; the payment is carried by the exit's confirmed-deposit counter.
- **The exit is paid net of the vault's withdrawal fee** (20 bps on the pinned
  chain). Its income is asserted against a 1% fee ceiling
  (`CURVY_FEE_CEILING_BPS`), and the fee one sweep actually paid is printed.

A direct shield is the Safe calling the Curvy aggregator through its node
management module, which forwards only to targets scoped into it. The cluster
grants that once per Safe — its nodes' and the client's extra identity's — when
`HOPRD_CURVY_SCOPE_AGGREGATOR` is set, as `up-curvy` does. A Safe without it
fails its first shield with `NonExistentKey()`.

## Running the end-to-end browser tests

```sh
just up                                  # build + cluster + servers + metrics + gen-config + client
just e2e                                 # every destination the client reports
just e2e --quick                         # short profile, a couple of minutes
just e2e --destination node-<peer-id>    # a single destination
just down
```

`just e2e` builds a sidecar image (node + obscura + the harness) and, for each
destination, connects the client, drives a real headless browser through the
tunnel — site loads, a Wikipedia link crawl, a Cloudflare speedtest, continuous
ICMP — then disconnects. Results land in `E2E_OUT_DIR/<UTC-timestamp>/` as
per-destination JSON plus a `summary.csv` that two runs can be diff'd on.

The browser has to sit inside the client container's network namespace, since
that is the only place the full-tunnel WireGuard routes exist, so the sidecar
joins it with `docker run --network container:gnosis_vpn-client`. The
orchestrator itself stays on the host and reaches `gnosis_vpn-ctl` via
`docker exec`.

Budget ~10 minutes per destination for the full profile (dominated by the two
speedtest passes), which is why it is a separate opt-in recipe rather than part
of `up`. See [`e2e/README.md`](e2e/README.md) for what each measurement means,
the env knobs, and the obscura caveats.

## Connectivity smoke-test / drain-tour / traffic scripts

```sh
just smoke-test              # validate connectivity on an already-connected tunnel
just drain-tour --out-dir …  # cycle every destination until account funding drains
just drain-report --run-dir …  # render a drain-tour run into a self-contained report.html
just wg-traffic --output …   # log WireGuard byte counters to CSV over time
just test-scripts            # offline bats suite for scripts/, no network
```

Standalone bash tooling under [`scripts/`](scripts/README.md), independent of
the container/e2e setup above — see that README for what each script checks and
how to fetch them onto a bare machine.

## Configuration variables

| Variable                  | Default                                                                   | Purpose                                         |
| ------------------------- | ------------------------------------------------------------------------- | ----------------------------------------------- |
| `HOPRD_DIR`               | `../hoprd`                                                                | Path to hoprd repo                              |
| `GVPN_SERVER_DIR`         | `../gnosis_vpn-server`                                                    | Path to gnosis_vpn-server repo                  |
| `GVPN_CLIENT_DIR`         | `../gnosis_vpn-client`                                                    | Path to gnosis_vpn-client repo                  |
| `CLUSTER_SIZE`            | `3`                                                                       | Number of HOPR nodes in localcluster            |
| `SERVER_COUNT`            | `1`                                                                       | Number of VPN server containers                 |
| `HOPS`                    | `1`                                                                       | Session hop count for destinations              |
| `DOCKER_NETWORK`          | `gnosis-vpn-testenv`                                                      | Docker network joining client to localcluster   |
| `DOCKER_NETWORK_SUBNET`   | `172.30.0.0/24`                                                           | Subnet for `DOCKER_NETWORK`                     |
| `DOCKER_NETWORK_GATEWAY`  | `172.30.0.1`                                                              | Gateway IP — also the cluster's P2P bind host   |
| `CLIENT_STATE_DIR`        | `/tmp/gnosis_vpn-testenv-state`                                           | Persistent worker state (identity keys, cache)  |
| `CLIENT_LOG_LEVEL`        | `warn,gnosis_vpn_root=debug,gnosis_vpn_lib=debug,gnosis_vpn_worker=debug` | RUST_LOG for the client                         |
| `CLIENT_WORKER_USER`      | `gnosisvpntestenv`                                                        | Host user for `up-client-on-host` (must exist)  |
| `CLIENT_LOG_FILE`         | `/tmp/gnosis_vpn-client.log`                                              | Log file for `up-client-on-host`                |
| `SERVER_LOG_LEVEL`        | `info`                                                                    | RUST_LOG for VPN server containers              |
| `CLUSTER_LOG_LEVEL`       | `info`                                                                    | RUST_LOG for the localcluster                   |
| `DATA_DIR`                | `/tmp/hopr-nodes`                                                         | Localcluster data directory                     |
| `METRICS_DATA_DIR`        | `/tmp/hopr-metrics-data`                                                  | VictoriaMetrics on-disk storage                 |
| `CONFIG_DIR`              | `/tmp/gnosis_vpn-testenv`                                                 | Generated config output directory               |
| `CHAIN_IMAGE`             | `…/bloklid-anvil:latest`                                                  | Blokli + Anvil container image                  |
| `LAN_IP`                  | auto-detected                                                             | Override for `up-on-network`'s LAN IP detection |
| `E2E_IMAGE`               | `gnosis_vpn-e2e`                                                          | Tag for the e2e browser sidecar image           |
| `E2E_OUT_DIR`             | `/tmp/gnosis_vpn-testenv-e2e`                                             | Parent directory for e2e run output             |
| `CLUSTER_ENABLE_PIX`      | unset                                                                     | Non-empty: `--enable-pix` + PIX client config   |
| `CLUSTER_PIX_POOL`        | `test`                                                                    | PIX pool: `test`, or `curvy` (see above)        |
| `CURVY_GATEWAY_PORT`      | `3900`                                                                    | Curvy gateway port (relayer, indexer)           |
| `HOPRD_BIN`               | `$HOPRD_DIR/result-hoprd/bin/hoprd` (`result-hoprd-pix-curvy` for curvy)  | hoprd node binary the localcluster spawns       |
| `LOCALCLUSTER_BIN`        | `$HOPRD_DIR/result-localcluster/bin/hoprd-localcluster`                   | Localcluster binary (override with `HOPRD_BIN`) |
| `SYSTEM_TEST_WORKER_USER` | `gnosisvpn`                                                               | Worker user for `system-tests` (created if new) |
| `SYSTEM_TEST_STATE_DIR`   | `/tmp/gnosis_vpn-testenv-system-tests`                                    | `system-tests` state home (wiped every run)     |
| `SYSTEM_TEST_LOG_LEVEL`   | `info,gnosis_vpn_root=debug,gnosis_vpn_lib=debug,gnosis_vpn_worker=debug` | RUST_LOG for the `system-tests` run             |

## Client state directory

The client container stores persistent state (identity keys, cache) under
`CLIENT_STATE_DIR`, bind-mounted into the container at `/var/lib/gnosisvpn`. The
container's entrypoint `chown`s it to the worker's internal user on startup, so
removing it later needs `sudo` (see below).

### Purging state

```sh
just purge-state
```

Deletes `CLIENT_STATE_DIR` after asking for a `yes` confirmation. Use this to
start with a clean identity after a failed run or when rotating keys.

## Running the client on the host (dev/debug)

```sh
just up-client-on-host
just client-logs-on-host
just client-stop-on-host
```

An alternative to `just up`/`client-start` that runs `gnosis_vpn-root` and
`gnosis_vpn-worker` as native host processes instead of in Docker — useful for
attaching a debugger or otherwise skipping the container network path. It also
runs the localcluster via `cluster-start-on-host` instead of `cluster-start`,
which binds P2P on `127.0.0.1` rather than `DOCKER_NETWORK_GATEWAY` — since
there's no containerized client to route it to over a bridge, `DOCKER_NETWORK`
isn't created or used at all in this mode, which also sidesteps the host
firewall / P2P reachability issue noted below entirely. This reintroduces the
routing-loop risk the container was built to avoid (see "Why the client runs in
its own container" above) if `gnosis_vpn-server` shares the host's egress, and
requires `CLIENT_WORKER_USER` to already exist as a system account
(`gnosis_vpn-root` drops privileges to it by uid/gid when spawning the worker —
its home directory doesn't matter). Don't run this alongside
`client-start`/`cluster-start` at the same time; they'd fight over
`CLIENT_STATE_DIR` and the default control socket. (Switching _between_
`cluster-start`/`cluster-start-on-host`/`cluster-start-on-network` sequentially
is fine — each detects the running cluster's actual `--p2p-host` via `status`
and transparently restarts it if it doesn't match what that recipe needs.)

## Running on the network (client on another machine)

```sh
just up-on-network
```

Brings up the localcluster and exit server(s) so they're reachable from another
physical machine on the LAN, instead of only from this host or its Docker
network — useful for testing against a real remote client without a container.

Unlike `up`/`up-client-on-host`, this recipe starts no client at all: the client
runs on the other machine, from its own `gnosis_vpn-client` checkout. It differs
from `cluster-start`/`cluster-start-on-host` only in what IP the localcluster
binds and announces its P2P host as — `hoprd-localcluster --p2p-host` uses the
same value for both, so it must be a real, reachable IP (see the note on
`DOCKER_NETWORK_GATEWAY` below); here that's the host's LAN-facing IP instead of
the Docker gateway or loopback. `LAN_IP` is auto-detected from the default route
(override it on multi-NIC hosts, or if detection picks the wrong interface).

`gen-config-on-network` bundles the files the other machine needs
(`client-on-network.toml`, `extra_id.id`, `extra_id.password`) into
`CONFIG_DIR/on-network/`. `up-on-network` finishes by printing:

- An `rsync` command to run _from the other machine_ that pulls
  `CONFIG_DIR/on-network/` from this host over SSH into
  `/tmp/gnosis_vpn-on-network`, plus a `chmod` to make it world-readable —
  `gnosis_vpn-worker` reads the identity file as an unprivileged user, so the
  bundle can't sit under a private home directory.
- Steps to build the client with `cargo build --release` and copy the resulting
  `gnosis_vpn-worker` binary into the worker user's home dir, `chown`ed to that
  user — the worker binary has the same unprivileged-user-can't-reach-it problem
  as the identity file above, since `target/release` sits under your home dir (a
  nix build wouldn't need this, its result lives in the world-readable
  `/nix/store`).
- The manual `gnosis_vpn-root` invocation to run there
  (`./target/release/gnosis_vpn-root --worker-binary
  ${worker_home}/gnosis_vpn-worker --state-home ${worker_home}`),
  with the identity/config/Blokli-URL/worker-user settings passed as CLI flags,
  pointing at the pulled bundle.
- The ports this host's firewall needs to allow inbound from the other machine —
  the same NixOS-firewall caveat as below applies, just against the LAN
  interface instead of the Docker bridge.

## Metrics stack

`just up` (and `just development-setup`) also starts a local metrics pipeline:

- **otelcol** — receives OTLP/HTTP on `127.0.0.1:4318` and forwards to
  VictoriaMetrics
- **VictoriaMetrics** — stores metrics and exposes a PromQL UI at
  `http://localhost:8428`

The client and server emit OpenTelemetry metrics to `127.0.0.1:4318`
automatically when the stack is up. Data is persisted under `METRICS_DATA_DIR`
between runs; `just down` stops both services but does not delete the data.

```sh
# Start/stop independently if needed
just metrics-start
just metrics-stop
```

## Port assignments

The client container publishes no ports — it only needs egress (to the
localcluster via `DOCKER_NETWORK_GATEWAY`, and to the exit node's WireGuard
tunnel via the HOPR mixnet, both outbound).

| Service                | Protocol | Host port   |
| ---------------------- | -------- | ----------- |
| HOPR node i            | TCP      | `3000 + i`  |
| HOPR P2P node i        | UDP      | `9000 + i`  |
| Blokli chain           | TCP      | `8080`      |
| VPN server i API       | TCP      | `8000 + i`  |
| VPN server i WireGuard | UDP      | `51821 + i` |
| otelcol OTLP/HTTP      | TCP      | `4318`      |
| VictoriaMetrics PromQL | TCP      | `8428`      |

## Utility recipes

| Recipe                     | What it does                                                                   |
| -------------------------- | ------------------------------------------------------------------------------ |
| `clean`                    | Removes all generated configs, data dirs, log files, and Nix results           |
| `reset`                    | `down` followed by `clean` — full wipe                                         |
| `logs`                     | `tail -f` cluster node logs and `docker logs -f` the client container          |
| `node-logs`                | `tail -f` only the hoprd node logs                                             |
| `summary`                  | Print `gnosis_vpn-ctl` usage and component commits/tags (runs as part of `up`) |
| `network-create`           | Creates `DOCKER_NETWORK` (idempotent; also runs as part of `cluster-start`)    |
| `network-remove`           | Removes `DOCKER_NETWORK` (runs as part of `clean`)                             |
| `up-pix`                   | `up` with PIX enabled on the cluster and in the client config — see above      |
| `up-curvy`                 | `up-pix` settling through a local Curvy deployment — see above                 |
| `curvy-stack-up`           | Start the Curvy deployment the `curvy` pool settles through                    |
| `curvy-stack-down`         | Stop it (also part of `down`)                                                  |
| `system-test-pix`          | Drive a PIX cycle and assert the exit's income — see above                     |
| `up-client-on-host`        | `up`, but the client runs natively on the host — see below                     |
| `build-e2e`                | Builds the e2e browser sidecar image (runs as part of `e2e`)                   |
| `cluster-start-on-host`    | Localcluster variant used by `up-client-on-host` — P2P on `127.0.0.1`          |
| `client-logs-on-host`      | `tail -f` the host-native client's log file                                    |
| `client-stop-on-host`      | Stops the host-native client                                                   |
| `up-on-network`            | Cluster + exit server reachable from another machine on the LAN — see below    |
| `cluster-start-on-network` | Localcluster variant used by `up-on-network` — P2P on the LAN IP               |
| `gen-config-on-network`    | Config variant used by `up-on-network`, targeting the exit server via LAN IP   |
| `smoke-test`               | Validate connectivity on an already-connected tunnel — see `scripts/README.md` |
| `drain-tour`               | Cycle every destination until account funding drains — see `scripts/README.md` |
| `drain-report`             | Render a `drain-tour` run directory into a self-contained `report.html`        |
| `wg-traffic`               | Log a WireGuard interface's byte counters to CSV over time                     |
| `test-scripts`             | Offline bats suite for `scripts/` (no network, uses fakes)                     |

On hosts running a default-deny host firewall (e.g. NixOS's
`networking.firewall`), the localcluster's P2P transport
(`DOCKER_NETWORK_GATEWAY:9000+i`) may be unreachable from the client container:
`gnosis_vpn-ctl status` (or the client logs) will show peers fetched
(`num_announced=3`) but never connected (`num_connected=0`), forever. If you hit
this, allow inbound UDP `9000..9000+CLUSTER_SIZE-1` from the testenv's Docker
bridge interface (`docker network inspect
$DOCKER_NETWORK` to find it) through
your host firewall.

## Notes

- The localcluster provisions one extra pre-funded HOPR identity
  (`--extra-identities 1`) that the gnosis_vpn-client uses to spin up its
  internal entry node — no manual funding required for local dev.
- The client config (`CONFIG_DIR/client.toml`) targets server-0 for both
  `[connection.bridge]` and `[connection.wg]`. Per-destination server selection
  is a planned client feature.
- `just gen-config` is idempotent against a running cluster and can be re-run to
  refresh configs without restarting anything.
- The localcluster's P2P host/announce address is `DOCKER_NETWORK_GATEWAY`, not
  `127.0.0.1` — `hoprd-localcluster --p2p-host` uses the same value for both the
  bind address and the on-chain announced multiaddr, so it must be a real,
  reachable IP (not `0.0.0.0`/`auto`). Since that gateway IP is a real interface
  on the host, native processes (e.g. `system-tests`) reach it exactly as they
  reached `127.0.0.1` before; containers on `DOCKER_NETWORK` reach it too.
- Exit-node (`gnosis_vpn-server`) containers are unaffected by this change and
  don't join `DOCKER_NETWORK` — the cluster already reaches their published host
  ports directly, as before.
- `build-cluster` builds `binary-hoprd-pix-test-x86_64-linux`, not the default
  `binary-hoprd`. gnosis_vpn-client builds `edgli` with `pix-test`
  (`hopr-lib/pix-secp256k1`) and enables PIX for the main tunnel session by
  default, while `binary-hoprd` takes hopr-lib's default `pix-bjj`. The curve is
  a network-wide invariant that nothing negotiates, so a `pix-bjj` exit refuses
  every session this client opens (`UnacceptablePixParams`, logged on the node
  as "refusing a client offering a PIX curve suite this node was not built
  for").
- The client's post-connect tunnel ping is hardcoded to `10.128.0.1`
  (`ping::Options::default()` in `core::runner::tunnel_ping_loop`) and does not
  read `[connection.ping] address`, which only the connect-time verification
  ping honours. `gnosis_vpn-server`'s `docker/wggvpn.conf` puts the exit's
  WireGuard interface on `10.129.0.1`, so that probe can never answer here and
  the client tears the tunnel down and reconnects roughly every 90 s ("tunnel
  ping exceeded max failures - reconnecting"). Connections still establish, so
  the system tests pass, but nothing stays up for long.
- PIX _settles_ only if the exit also runs the `Pix` strategy, which is opt-in
  and not part of hoprd's default strategy set.
  `hoprd-localcluster --enable-pix` adds it, but its demo geometry caps the
  accepted per-SSA quota at 1 MiB, and gnosis_vpn-client offers hopr-lib's
  default ≈649 MiB — so that flag cannot serve this client as it stands, and the
  recipes don't pass it. Without it the exit accepts PIX sessions (its default
  quota window covers the client's offer) but never observes the deposits the
  client makes.
