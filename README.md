# Gnosis VPN test environment

Local development and system-test stack for Gnosis VPN. Orchestrates a HOPR localcluster, one or more containerised Gnosis VPN server (exit node) instances, and a containerised Gnosis VPN client against them.

## Prerequisites

- [Nix](https://nixos.org/) with flakes enabled
- [just](https://just.systems/) (system-level; must be available outside any Nix shell to avoid nesting issues)
- Docker (or Podman / Apple `container`)
- Sibling repos checked out at the paths below (overridable)

## Why the client runs in its own container

The client and the exit-node servers used to run on the same host network. The client's full-tunnel WireGuard route (`0.0.0.0/1` + `128.0.0.0/1` via `wg0`) is installed into the _main routing table_, so once it was up, it captured every outbound packet on that host — including the exit-node container's own Docker-NATed egress to the real internet. That packet would get pulled back into the tunnel instead of leaving, producing a routing loop rather than internet access.

Running the client in its own container fixes this architecturally: its full-tunnel route now lives only in the client container's own network namespace, so it can no longer capture the exit-node container's (or the host's) traffic. The localcluster still runs natively on the host; the client container reaches it over a dedicated Docker network (see below).

## Sibling repo paths

By default the recipes expect these repos next to each other:

```
parent/
  gnosis_vpn-testenv/   ← this repo
  hoprd/
  gnosis_vpn-server/
  gnosis_vpn-client/
```

Override any path via environment variable — useful in CI where repos are checked out independently:

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

`just development-setup` builds all components, then brings the whole stack up (localcluster, VPN server(s), metrics, generated config, and the client container); the client container gets its WireGuard/routing privileges from `--cap-add=NET_ADMIN` instead of host root, so no `sudo` is needed for that.

`just up` (and thus `development-setup`) finishes by printing a summary: the `gnosis_vpn-ctl` commands to control the client and each component's checked-out commit (and tag, if any) for `gnosis_vpn-client`/`gnosis_vpn-server`/`hoprd`. Re-print it anytime with `just summary`.

`just up` does the same without the build step; useful for scripting and CI when components are pre-built.

## Issuing client commands

Once the stack is up, control the running client via `gnosis_vpn-ctl` inside its container — the client image symlinks the binary onto `PATH` for exactly this purpose:

```sh
docker exec -it gnosis_vpn-client gnosis_vpn-ctl status
docker exec -it gnosis_vpn-client gnosis_vpn-ctl connect <destination-id>
docker exec -it gnosis_vpn-client gnosis_vpn-ctl --help
```

`gnosis_vpn-ctl` talks to the client's `gnosis_vpn-root` process over `/var/run/gnosisvpn.sock` inside the container, so no `--socket-path` flag is needed when run this way.

## Running system tests

```sh
just up             # build + cluster + servers + metrics + gen-config
just system-tests   # delegates to gnosis_vpn-client's system-tests with generated artifacts
just down
```

## Running the end-to-end browser tests

```sh
just up                                  # build + cluster + servers + metrics + gen-config + client
just e2e                                 # every destination the client reports
just e2e --fast                         # short profile, a couple of minutes
just e2e --destination node-<peer-id>    # a single destination
just down
```

`just e2e` builds a sidecar image (node + obscura + the harness) and, for each destination, connects the client, drives a real headless browser through the tunnel — site loads, a Wikipedia link crawl, a Cloudflare speedtest, continuous ICMP — then disconnects. Results land in `E2E_OUT_DIR/<UTC-timestamp>/` as per-destination JSON plus a `summary.csv` that two runs can be diff'd on.

The browser has to sit inside the client container's network namespace, since that is the only place the full-tunnel WireGuard routes exist, so the sidecar joins it with `docker run --network container:gnosis_vpn-client`. The orchestrator itself stays on the host and reaches `gnosis_vpn-ctl` via `docker exec`.

Budget ~10 minutes per destination for the full profile (dominated by the two speedtest passes), which is why it is a separate opt-in recipe rather than part of `up`. See [`e2e/README.md`](e2e/README.md) for what each measurement means, the env knobs, and the obscura caveats.

## Regression suite

[docs/regression-catalogue.md](docs/regression-catalogue.md) describes 32 tests distilled from the 2026-09 throughput and reliability investigations; [scripts/suite/](scripts/suite/README.md) implements them as pytest tests (`scripts/suite/tests/test_tNN_<name>.py`) with a shared library, and a version-matrix driver.

```sh
just up-nobuild                                   # pre-built binaries/images: cluster + server + target + client
just test t04                                     # one catalogue test against the live stack
just suite --fast                                 # the one run, shorter durations (--very-fast, --only, --skip, --knob)
just matrix scripts/suite/cells/example.cells     # one stack per version/config cell
just suite-selftest                               # offline: library unit tests + every probe against every target service on loopback
just suite --client gnosis_vpn-client --dest node-0 --target 203.0.113.7 --no-cluster   # production network: any client, any host running docker/target
just nightly                                      # the daily battery: build the latest of every component, up, suite --fast, down
```

What the suite added to the stack (each is a justfile variable with the old behaviour as default):

| Variable | Purpose |
| --- | --- |
| `HOPRD_BIN`, `LOCALCLUSTER_BIN` | run a stock release binary / a cargo build / a different hoprd per cell instead of the nix out-links |
| `CLUSTER_ENV` | environment for every cluster node (`HOPR_INTERNAL_IN_PACKET_PIPELINE_CONCURRENCY=64`, catalogue T25-knob-ab) |
| `CLUSTER_LATENCY` | `hoprd-localcluster --latency` (per-link delay, catalogue T09-impairment-ladder/T17-latency-matrix) |
| `CLUSTER_CHANNEL_MANAGEMENT`, `CLUSTER_FUNDING`, `EXTRA_IDENTITIES` | localcluster knobs; `EXTRA_IDENTITIES=2` funds a second client |
| `CLIENT_IMAGE`, `SERVER_IMAGE` | image tags per cell (T26-version-matrix) |
| `CLIENT_EXTRA_ARGS`, `CLIENT_EXTRA_ENV`, `CLIENT_SYSCTL`, `CLIENT_AUTOSTART` | `--allow-insecure` (T30-hopcount-ab), env knobs (T25-knob-ab), congestion control (T32-congestion-control), worker keepalive |
| `LOG_MAX_SIZE`, `LOG_MAX_FILE` | bounded container logs for every container (soak safety) |
| `HOPS0_ALSO` | also generate 0-hop `node-N-h0` destinations (T30-hopcount-ab) |
| `TARGET_IMAGE`, `TARGET_NAME` | the in-cluster traffic target (`just target-start`): sized HTTP download/upload, UDP echo, one-way stream server, two-way call server |

New recipes: `nightly`, `build-client-glibc DIR TAG` and `build-server-glibc BIN TAG` (Ubuntu-based images from cargo builds or an unpacked `.deb`, no Nix), `build-target`, `target-start`/`target-stop`, `client2-start`/`client2-stop`, `up-nobuild`, `cluster-restart`, `test`, `suite`, `suite-selftest`, `matrix`. `gen-config` now saves every extra identity as `extra_id_<i>.*` (keeping `extra_id.*` for the client).

## Connectivity smoke-test / drain-tour / traffic scripts

```sh
just smoke-test              # validate connectivity on an already-connected tunnel
just drain-tour --out-dir …  # cycle every destination until account funding drains
just drain-report --run-dir …  # render a drain-tour run into a self-contained report.html
just wg-traffic --output …   # log WireGuard byte counters to CSV over time
just test-scripts            # offline bats suite for scripts/, no network
```

Standalone bash tooling under [`scripts/`](scripts/README.md), independent of the container/e2e setup above — see that README for what each script checks and how to fetch them onto a bare machine.

## Configuration variables

| Variable                 | Default                                                                   | Purpose                                         |
| ------------------------ | ------------------------------------------------------------------------- | ----------------------------------------------- |
| `HOPRD_DIR`              | `../hoprd`                                                                | Path to hoprd repo                              |
| `GVPN_SERVER_DIR`        | `../gnosis_vpn-server`                                                    | Path to gnosis_vpn-server repo                  |
| `GVPN_CLIENT_DIR`        | `../gnosis_vpn-client`                                                    | Path to gnosis_vpn-client repo                  |
| `CLUSTER_SIZE`           | `3`                                                                       | Number of HOPR nodes in localcluster            |
| `SERVER_COUNT`           | `1`                                                                       | Number of VPN server containers                 |
| `HOPS`                   | `1`                                                                       | Session hop count for destinations              |
| `DOCKER_NETWORK`         | `gnosis-vpn-testenv`                                                      | Docker network joining client to localcluster   |
| `DOCKER_NETWORK_SUBNET`  | `172.30.0.0/24`                                                           | Subnet for `DOCKER_NETWORK`                     |
| `DOCKER_NETWORK_GATEWAY` | `172.30.0.1`                                                              | Gateway IP — also the cluster's P2P bind host   |
| `CLIENT_STATE_DIR`       | `/tmp/gnosis_vpn-testenv-state`                                           | Persistent worker state (identity keys, cache)  |
| `CLIENT_LOG_LEVEL`       | `warn,gnosis_vpn_root=debug,gnosis_vpn_lib=debug,gnosis_vpn_worker=debug` | RUST_LOG for the client                         |
| `CLIENT_WORKER_USER`     | `gnosisvpntestenv`                                                        | Host user for `up-client-on-host` (must exist)  |
| `CLIENT_LOG_FILE`        | `/tmp/gnosis_vpn-client.log`                                              | Log file for `up-client-on-host`                |
| `SERVER_LOG_LEVEL`       | `info`                                                                    | RUST_LOG for VPN server containers              |
| `CLUSTER_LOG_LEVEL`      | `info`                                                                    | RUST_LOG for the localcluster                   |
| `DATA_DIR`               | `/tmp/hopr-nodes`                                                         | Localcluster data directory                     |
| `METRICS_DATA_DIR`       | `/tmp/hopr-metrics-data`                                                  | VictoriaMetrics on-disk storage                 |
| `CONFIG_DIR`             | `/tmp/gnosis_vpn-testenv`                                                 | Generated config output directory               |
| `CHAIN_IMAGE`            | `…/bloklid-anvil:latest`                                                  | Blokli + Anvil container image                  |
| `LAN_IP`                 | auto-detected                                                             | Override for `up-on-network`'s LAN IP detection |
| `E2E_IMAGE`              | `gnosis_vpn-e2e`                                                          | Tag for the e2e browser sidecar image           |
| `E2E_OUT_DIR`            | `/tmp/gnosis_vpn-testenv-e2e`                                             | Parent directory for e2e run output             |

## Client state directory

The client container stores persistent state (identity keys, cache) under `CLIENT_STATE_DIR`, bind-mounted into the container at `/var/lib/gnosisvpn`. The container's entrypoint `chown`s it to the worker's internal user on startup, so removing it later needs `sudo` (see below).

### Purging state

```sh
just purge-state
```

Deletes `CLIENT_STATE_DIR` after asking for a `yes` confirmation. Use this to start with a clean identity after a failed run or when rotating keys.

## Running the client on the host (dev/debug)

```sh
just up-client-on-host
just client-logs-on-host
just client-stop-on-host
```

An alternative to `just up`/`client-start` that runs `gnosis_vpn-root` and `gnosis_vpn-worker` as native host processes instead of in Docker — useful for attaching a debugger or otherwise skipping the container network path. It also runs the localcluster via `cluster-start-on-host` instead of `cluster-start`, which binds P2P on `127.0.0.1` rather than `DOCKER_NETWORK_GATEWAY` — since there's no containerized client to route it to over a bridge, `DOCKER_NETWORK` isn't created or used at all in this mode, which also sidesteps the host firewall / P2P reachability issue noted below entirely. This reintroduces the routing-loop risk the container was built to avoid (see "Why the client runs in its own container" above) if `gnosis_vpn-server` shares the host's egress, and requires `CLIENT_WORKER_USER` to already exist as a system account (`gnosis_vpn-root` drops privileges to it by uid/gid when spawning the worker — its home directory doesn't matter). Don't run this alongside `client-start`/`cluster-start` at the same time; they'd fight over `CLIENT_STATE_DIR` and the default control socket. (Switching _between_ `cluster-start`/`cluster-start-on-host`/`cluster-start-on-network` sequentially is fine — each detects the running cluster's actual `--p2p-host` via `status` and transparently restarts it if it doesn't match what that recipe needs.)

## Running on the network (client on another machine)

```sh
just up-on-network
```

Brings up the localcluster and exit server(s) so they're reachable from another physical machine on the LAN, instead of only from this host or its Docker network — useful for testing against a real remote client without a container.

Unlike `up`/`up-client-on-host`, this recipe starts no client at all: the client runs on the other machine, from its own `gnosis_vpn-client` checkout. It differs from `cluster-start`/`cluster-start-on-host` only in what IP the localcluster binds and announces its P2P host as — `hoprd-localcluster --p2p-host` uses the same value for both, so it must be a real, reachable IP (see the note on `DOCKER_NETWORK_GATEWAY` below); here that's the host's LAN-facing IP instead of the Docker gateway or loopback. `LAN_IP` is auto-detected from the default route (override it on multi-NIC hosts, or if detection picks the wrong interface).

`gen-config-on-network` bundles the files the other machine needs (`client-on-network.toml`, `extra_id.id`, `extra_id.password`) into `CONFIG_DIR/on-network/`. `up-on-network` finishes by printing:

- An `rsync` command to run _from the other machine_ that pulls `CONFIG_DIR/on-network/` from this host over SSH into `/tmp/gnosis_vpn-on-network`, plus a `chmod` to make it world-readable — `gnosis_vpn-worker` reads the identity file as an unprivileged user, so the bundle can't sit under a private home directory.
- Steps to build the client with `cargo build --release` and copy the resulting `gnosis_vpn-worker` binary into the worker user's home dir, `chown`ed to that user — the worker binary has the same unprivileged-user-can't-reach-it problem as the identity file above, since `target/release` sits under your home dir (a nix build wouldn't need this, its result lives in the world-readable `/nix/store`).
- The manual `gnosis_vpn-root` invocation to run there (`./target/release/gnosis_vpn-root --worker-binary ${worker_home}/gnosis_vpn-worker --state-home ${worker_home}`), with the identity/config/Blokli-URL/worker-user settings passed as CLI flags, pointing at the pulled bundle.
- The ports this host's firewall needs to allow inbound from the other machine — the same NixOS-firewall caveat as below applies, just against the LAN interface instead of the Docker bridge.

## Metrics stack

`just up` (and `just development-setup`) also starts a local metrics pipeline:

- **otelcol** — receives OTLP/HTTP on `127.0.0.1:4318` and forwards to VictoriaMetrics
- **VictoriaMetrics** — stores metrics and exposes a PromQL UI at `http://localhost:8428`

The client and server emit OpenTelemetry metrics to `127.0.0.1:4318` automatically when the stack is up. Data is persisted under `METRICS_DATA_DIR` between runs; `just down` stops both services but does not delete the data.

```sh
# Start/stop independently if needed
just metrics-start
just metrics-stop
```

## Port assignments

The client container publishes no ports — it only needs egress (to the localcluster via `DOCKER_NETWORK_GATEWAY`, and to the exit node's WireGuard tunnel via the HOPR mixnet, both outbound).

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

On hosts running a default-deny host firewall (e.g. NixOS's `networking.firewall`), the localcluster's P2P transport (`DOCKER_NETWORK_GATEWAY:9000+i`) may be unreachable from the client container: `gnosis_vpn-ctl status` (or the client logs) will show peers fetched (`num_announced=3`) but never connected (`num_connected=0`), forever. If you hit this, allow inbound UDP `9000..9000+CLUSTER_SIZE-1` from the testenv's Docker bridge interface (`docker network inspect $DOCKER_NETWORK` to find it) through your host firewall.

## Notes

- The localcluster provisions one extra pre-funded HOPR identity (`--extra-identities 1`) that the gnosis_vpn-client uses to spin up its internal entry node — no manual funding required for local dev.
- The client config (`CONFIG_DIR/client.toml`) targets server-0 for both `[connection.bridge]` and `[connection.wg]`. Per-destination server selection is a planned client feature.
- `just gen-config` is idempotent against a running cluster and can be re-run to refresh configs without restarting anything.
- The localcluster's P2P host/announce address is `DOCKER_NETWORK_GATEWAY`, not `127.0.0.1` — `hoprd-localcluster --p2p-host` uses the same value for both the bind address and the on-chain announced multiaddr, so it must be a real, reachable IP (not `0.0.0.0`/`auto`). Since that gateway IP is a real interface on the host, native processes (e.g. `system-tests`) reach it exactly as they reached `127.0.0.1` before; containers on `DOCKER_NETWORK` reach it too.
- Exit-node (`gnosis_vpn-server`) containers are unaffected by this change and don't join `DOCKER_NETWORK` — the cluster already reaches their published host ports directly, as before.
