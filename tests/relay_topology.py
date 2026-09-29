#!/usr/bin/env python3
"""Bring up (or check) a relay-scaling topology for T33-relay-baseline and T34-single-relay-scaling.

    just relay-topology paired 5     # T33: client k -> relay k -> exit k, 5 of each
    just relay-topology shared 5     # T34: clients 1..5 -> one relay -> exits 1..5
    just relay-topology-check        # hold the live channel graph against CONFIG_DIR/relay-topology.json

`up` takes the whole stack down first (like `just down`), starts a localcluster with `--channel-management none`
and one pre-funded identity per client, one VPN server per client, the traffic target, then opens each exit's one
channel (to its relay) through the exit's REST API, starts the clients with their own config (client-<k>.toml: one
destination, own server ports, a strategy that opens one channel to the assigned relay), and waits until the chain
shows exactly the topology. The layout and the addresses land in CONFIG_DIR/relay-topology.json. See
suitelib/relaytopo.py for why one channel per client and per exit pins both directions to the relay."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from suitelib import relaytopo, shell  # noqa: E402
from suitelib.cluster import Cluster  # noqa: E402
from suitelib.config import Config  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def say(*a):
    print(time.strftime("%H:%M:%S", time.gmtime()), *a, flush=True)


def just(env, *recipes, timeout=1800):
    say("just", *recipes)
    r = shell.run(["just", *recipes], timeout=timeout, env=env, cwd=str(REPO))
    tail = (r.stdout + r.stderr).strip().splitlines()[-6:]
    for line in tail:
        print("   ", line, flush=True)
    if r.returncode != 0:
        raise SystemExit(f"just {' '.join(recipes)} failed (rc {r.returncode})")
    return r


def full_topology(cluster, node):
    d = cluster.api_json(node, "GET", "/api/v4/channels?fullTopology=true", default={}) or {}
    return d.get("all", [])


def open_exit_channels(cluster, topo, amount, timeout):
    """One channel per exit, to its relay (T34's exits all go to the same relay). Idempotent: an exit whose channel is
    already Open is left alone, so a re-run after a partial failure does not open a second one."""
    pending = {c["exit"]: c for c in topo["clients"]}
    t0 = time.time()
    while pending:
        for ex, c in list(pending.items()):
            outs = [x for x in cluster.open_outgoing(ex) if str(x.get("peerAddress", "")).lower() == c["relay_address"].lower()]
            if outs:
                say(f"node-{ex} (exit) -> node-{c['relay']} (relay): Open")
                del pending[ex]
                continue
            r = cluster.api(ex, "POST", "/api/v4/channels", {"destination": c["relay_address"], "amount": amount}, timeout=120)
            say(f"node-{ex} (exit) -> node-{c['relay']} (relay): open {amount}: {r.strip()[:160]}")
        if pending:
            if time.time() - t0 > timeout:
                raise SystemExit(f"exit channels not open after {timeout}s: {sorted(pending)}")
            time.sleep(10)


def up(args):
    lay = relaytopo.layout(args.mode, args.n)
    env = dict(os.environ)
    env.update(CLUSTER_SIZE=str(lay["cluster_size"]), CLUSTER_CHANNEL_MANAGEMENT="none", EXTRA_IDENTITIES=str(lay["n"]),
               CLIENT_COUNT=str(lay["n"]), SERVER_COUNT=str(lay["n"]))
    down_env = dict(env, SERVER_COUNT=str(max(8, lay["n"])))      # whatever ran before, its servers go too
    just(down_env, "down", timeout=600)
    try:
        just(env, "metrics-start", "cluster-start", "cluster-wait", "server-start", "gen-config", "target-start", timeout=1800)
    except SystemExit:
        # the earliest node error, not the localcluster's timeout: a config the hoprd binary rejects reads as /startedz timing out
        log = Path(env.get("DATA_DIR") or "/tmp/hopr-nodes") / "logs" / "hoprd_0.log"
        errs = [line for line in (log.read_text(errors="replace").splitlines() if log.exists() else []) if "ERROR" in line]
        for line in errs[:3]:
            say("  node-0:", line[:300])
        raise
    cfg = Config(env)
    cluster = Cluster(cfg)
    status = cluster.status()
    if not status or status.get("state") != "running":
        raise SystemExit(f"cluster not running: {status and status.get('state')}")
    topo = relaytopo.with_addresses(lay, status)
    topo["created"] = time.strftime("%FT%TZ", time.gmtime())
    topo["exit_channel_funding"] = args.funding
    topo["client_image"] = env.get("CLIENT_IMAGE", "")
    topo["hoprd_bin"] = env.get("HOPRD_BIN", "")
    pix_on = bool(env.get("CLUSTER_ENABLE_PIX"))
    for c in topo["clients"]:
        (cfg.config_dir / c["config"]).write_text(relaytopo.render_client_config(REPO / "templates", c, pix_on=pix_on))
    relaytopo.save(cfg.config_dir, topo)
    say(f"layout {args.mode} n={lay['n']}: relays {lay['relays']} exits {lay['exits']} -> {cfg.config_dir / relaytopo.TOPOLOGY_FILE}")
    open_exit_channels(cluster, topo, args.funding, args.timeout)
    state = env.get("CLIENT_STATE_DIR") or "/tmp/gnosis_vpn-testenv-state"
    for c in topo["clients"]:
        sd = state if c["k"] == 1 else f"{state}-{c['k']}"
        just(env, "client-start-one", c["name"], sd, str(c["extra"]), c["config"], timeout=600)
    say("waiting for every client's channel to its relay (the client's strategy opens it on chain)")
    t0 = time.time()
    while True:
        problems, summary = relaytopo.check_channels(topo, full_topology(cluster, topo["relays"][0]))
        if not problems:
            break
        if time.time() - t0 > args.timeout:
            for p in problems:
                say("  ", p)
            raise SystemExit(f"topology not reached after {args.timeout}s")
        time.sleep(15)
    say(f"topology reached after {int(time.time() - t0)}s:")
    for who, chs in summary.items():
        say(f"   {who}: {chs}")
    topo["ready"] = time.strftime("%FT%TZ", time.gmtime())
    relaytopo.save(cfg.config_dir, topo)


def check(args):
    cfg = Config()
    topo = relaytopo.load(cfg.config_dir)
    if topo is None:
        raise SystemExit(f"no {cfg.config_dir / relaytopo.TOPOLOGY_FILE}; run `just relay-topology MODE N` first")
    cluster = Cluster(cfg)
    problems, summary = relaytopo.check_channels(topo, full_topology(cluster, topo["relays"][0]))
    print(json.dumps({"mode": topo["mode"], "n": topo["n"], "problems": problems, "channels_out": summary}, indent=2))
    raise SystemExit(1 if problems else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up", help="take the stack down and bring a topology up")
    u.add_argument("mode", choices=relaytopo.MODES)
    u.add_argument("n", type=int)
    u.add_argument("--funding", default=os.environ.get("RELAY_TOPO_FUNDING", "1 wxHOPR"),
                   help="stake of each exit's channel to its relay (default 1 wxHOPR, the localcluster's per-channel funding)")
    u.add_argument("--timeout", type=int, default=900, help="seconds to wait for the channels")
    sub.add_parser("check", help="hold the live channel graph against the saved topology")
    args = ap.parse_args()
    (up if args.cmd == "up" else check)(args)


if __name__ == "__main__":
    main()
