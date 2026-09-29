# Scripts

Operational/diagnostic tooling for a running Gnosis VPN client. All are
standalone bash - no build step.

The scripts in [`testenv/`](testenv/) are a different thing: they are the
justfile's recipe bodies, not operator tooling, and are documented at the bottom
of this file.

## Fetching just these scripts onto a fresh machine

`vpn-drain-tour.sh` sources `vpn-smoke-test.sh` for shared helpers, so grab all
three together:

```bash
mkdir -p gnosis-vpn-scripts && cd gnosis-vpn-scripts && \
for f in vpn-smoke-test.sh vpn-drain-tour.sh vpn-drain-report.sh; do
  curl -fsSL -o "$f" "https://raw.githubusercontent.com/gnosis/gnosis_vpn-testenv/main/scripts/$f"
done && \
chmod +x vpn-smoke-test.sh vpn-drain-tour.sh vpn-drain-report.sh
```

On a fresh Debian/Ubuntu box, `./vpn-drain-tour.sh --install-deps ...` installs
the missing `jq`/`curl`/`ping`/`awk` packages via `apt-get` on first run;
`gnosis_vpn-ctl` itself still needs to be installed/built separately (it's the
client's own binary, not an apt package).

## `vpn-smoke-test.sh`

Validates connectivity on an already-connected tunnel: gateway ping/loss, path
MTU, DNS, HTTPS reachability, sized downloads, a sustained transfer, egress
IP/geo, and an IPv6 leak check. `./vpn-smoke-test.sh --help` for options.

## `vpn-drain-tour.sh`

Each round, connects to every configured destination once (best
exit-capacity/latency first, recording why any destination can't connect),
smoke-tests it, then repeats rounds until `gnosis_vpn-ctl balance` reports
funding as `Empty` or nothing connects. Writes raw `runs.jsonl`/`metrics.csv` to
an output directory. `./vpn-drain-tour.sh --help` for options.

## `vpn-drain-report.sh`

Turns a `vpn-drain-tour.sh` run directory into one self-contained `report.html`:
a cross-destination comparison table, an outcome heatmap, latency/throughput
trend lines, a funding drain timeline, and per-destination histogram detail
sections - printable straight to PDF from a browser.
`./vpn-drain-report.sh --run-dir <dir>`.

## `wg-traffic.sh`

Samples a WireGuard interface's byte counters on an interval and logs per-window
totals to CSV. `./wg-traffic.sh --help` for options.

## `testenv/`

The `justfile`'s orchestration logic, one file per domain (`cluster.sh`,
`client.sh`, `server.sh`, `config.sh`, `curvy.sh`, `metrics.sh`, `summary.sh`,
`network.sh`, `logs.sh`, `system-tests.sh`, `e2e-on-network.sh`), each
dispatching on a subcommand and sharing `common.sh`. Unlike the scripts above
they are not meant to be fetched or run on their own: `just` is the entry point,
and `set export := true` hands every justfile variable to them as an environment
variable, which each file declares at the top with `: "${VAR:?}"`.

`just --list` remains the UI - to find the code behind a recipe, read the one
line the recipe dispatches to.

## Tests

`scripts/tests/*.bats` cover the scripts above offline: `curl`/`ping`/
`gnosis_vpn-ctl`/`apt-get`/`sudo` fakes in `scripts/tests/fakes/` for the VPN
scripts, fake sysfs interfaces (`scripts/tests/helpers.bash`) for
`wg-traffic.sh`, and a fake `hoprd-localcluster` for the `testenv-*.bats`
suites, which cover the derivable parts of `testenv/` (LAN IP resolution, status
and node-config parsing, config rendering, component versions) and leave
container orchestration alone. Run with `bats scripts/tests/` (or
`just test-scripts`); CI runs the same suite on every PR from a branch in this
repo (fork PRs skip it).
