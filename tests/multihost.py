#!/usr/bin/env python3
"""The testenv across several machines: chain, relays, exits and clients each on a machine of their own, or any of
them sharing one. Run on the clients' machine (the suite docker-execs the clients there):

    just multihost-check HOSTS                # what each machine has: binaries, images, repo, reachability
    just multihost-up HOSTS paired 5          # T33-relay-baseline's topology across the machines
    just multihost-up HOSTS shared 5          # T34-single-relay-scaling's
    just multihost-up HOSTS standard 5        # T22-concurrent-clients': 2 relays, 1 exit, full mesh, 5 clients
    just multihost-test HOSTS t33             # the test against it (sources CONFIG_DIR/multihost.env)
    just multihost-down HOSTS

HOSTS is a TOML file (multihost/hosts.example.toml): per role an `ssh` target (or "local") and the `addr` every other
machine and the clients reach it at, plus the paths of its binaries.

How a stack comes up. The chain machine runs the chain container (Anvil + Blokli) published on its `addr`. The relays'
and the exits' machines each run a hoprd-localcluster with `--chain-url` pointing at it, `--p2p-host`/`--api-host`
their own `addr`, `--channel-management none`, one after the other (both fund from the chain's one dev account).
Each localcluster pre-announces its nodes on chain right after their Safes, so every node finds every other through
Blokli. The relays' cluster also mints the clients' identities (`--extra-identities`). The exits' machine runs the VPN
servers (one per client in the relay topologies) and the traffic target, as the single-machine stack does. Then the
merged status (every node with a global id: relays 0.., exits after them) lands in CONFIG_DIR/multihost.json, the
clients start here, and the topology's channels are opened:

- paired / shared: tests/suitelib/relaytopo.py (one channel per client and per exit, to the assigned relay);
- standard: every node opens a channel to every other node (the localcluster's `api` full mesh), the clients use
  their own strategy, as on the single-machine stack.

CONFIG_DIR/multihost.env holds what the suite needs: MULTIHOST_STATUS, MULTIHOST_SSH_OPTS, TARGET_HOST (the target's
address behind the exit), DEST, CLUSTER_SIZE."""
import argparse
import json
import os
import secrets
import shlex
import string
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relay_topology import full_topology, just, open_exit_channels, say  # noqa: E402
from suitelib import relaytopo  # noqa: E402
from suitelib.cluster import Cluster  # noqa: E402
from suitelib.config import Config  # noqa: E402
from suitelib.hosts import Host  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ROLES = ("chain", "relays", "exits", "clients")
CHAIN_PORT = 8080
# API and P2P port bases per node role, distinct so relays and exits can share a machine
PORTS = {"relays": (3000, 9000), "exits": (3100, 9100)}
DATA = {"relays": "/tmp/hopr-mh-relays", "exits": "/tmp/hopr-mh-exits"}
CHAIN_NAME = "hopr-chain"
MODES = ("paired", "shared", "standard")


def load_hosts(path):
    d = tomllib.loads(Path(path).read_text())
    opts = d.get("ssh", {}).get("options", "")
    roles = {}
    for r in ROLES:
        if r not in d.get("roles", {}):
            raise SystemExit(f"{path}: [roles.{r}] missing")
        x = dict(d["roles"][r])
        x["host"] = Host(x.get("ssh"), opts)
        if r != "clients" and not x.get("addr"):
            raise SystemExit(f"{path}: roles.{r}.addr missing (the address the other machines reach it at)")
        roles[r] = x
    if not roles["clients"]["host"].local:
        raise SystemExit("run this on the clients' machine: roles.clients.ssh must be \"local\"")
    return opts, roles


def layout(mode, n):
    if mode in relaytopo.MODES:
        return relaytopo.layout(mode, n)
    # standard: T22-concurrent-clients' stack, two relays and one exit in a full mesh, n clients on their own strategy
    return {"mode": "standard", "n": int(n), "cluster_size": 3, "relays": [0, 1], "exits": [2], "clients": []}


def chain_url(roles):
    return f"http://{roles['chain']['addr']}:{CHAIN_PORT}"


def start_chain(roles):
    c = roles["chain"]
    h, image = c["host"], c.get("chain_image", "europe-west3-docker.pkg.dev/hoprassociation/docker-images/bloklid-anvil:latest")
    h.run(f"docker rm -f {CHAIN_NAME} >/dev/null 2>&1; for i in $(seq 1 30); do docker container inspect {CHAIN_NAME} >/dev/null 2>&1 || break; sleep 1; done")
    r = h.run(f"docker run -d --rm --name {CHAIN_NAME} --platform linux/amd64 -p {c['addr']}:{CHAIN_PORT}:{CHAIN_PORT} {image}", timeout=300)
    if r.returncode:
        raise SystemExit(f"chain container did not start on {h}: {r.stderr.strip()[:300]}")
    t0 = time.time()
    while roles["clients"]["host"].out(f"curl -s -o /dev/null -m 5 -w '%{{http_code}}' {chain_url(roles)}/", timeout=20) in ("", "000"):
        if time.time() - t0 > 300:
            raise SystemExit(f"Blokli at {chain_url(roles)} not answering after 300 s")
        time.sleep(3)
    say(f"chain on {h} at {chain_url(roles)}")


def stop_cluster(roles, role):
    r = roles[role]
    h, lc, hoprd, data = r["host"], r["localcluster_bin"], r["hoprd_bin"], DATA[role]
    # anchored at the binaries' paths: the ssh shell running this line starts with "sh"/"bash", so it never matches itself
    h.run(f"pkill -TERM -f '^{lc} .*--data-dir {data}' 2>/dev/null; "
          f"for i in $(seq 1 30); do pgrep -f '^{lc} .*--data-dir {data}' >/dev/null || break; sleep 1; done; "
          f"pkill -KILL -f '^{lc} .*--data-dir {data}' 2>/dev/null; pkill -KILL -f '^{hoprd} .*{data}' 2>/dev/null; rm -rf {data}; true",
          timeout=120)


def start_cluster(roles, role, size, extras, token):
    r = roles[role]
    h, lc, data = r["host"], r["localcluster_bin"], DATA[role]
    api, p2p = PORTS[role]
    args = (f"--hoprd-bin {r['hoprd_bin']} --chain-url {chain_url(roles)} --size {size} --p2p-host {r['addr']} "
            f"--p2p-port-base {p2p} --api-host {r['addr']} --api-port-base {api} --api-token {token} --data-dir {data} "
            f"--channel-management none --funding-amount '1 wxHOPR' --extra-identities {extras}")
    # the background job alone redirected, not an `a && b && c &` list: a backgrounded list keeps the ssh session's
    # stdout open and the call returned only at its timeout
    h.run(f"rm -rf {data}; mkdir -p {data}/logs; cd /tmp; setsid nohup env RUST_LOG=info {lc} {args} "
          f"> {data}/logs/localcluster.log 2>&1 < /dev/null & echo started", timeout=60)
    say(f"{role}: localcluster of {size} node(s) on {h} (p2p {r['addr']}:{p2p}+, api :{api}+)")
    t0 = time.time()
    while True:
        raw = h.out(f"{lc} status --data-dir {data}", timeout=60)
        try:
            st = json.loads(raw)
        except ValueError:
            st = {}
        if st.get("state") == "running":
            say(f"{role}: running after {int(time.time() - t0)}s")
            return st
        alive = h.ok(f"pgrep -f '^{lc} .*--data-dir {data}' >/dev/null", timeout=30)
        if st.get("state") == "failed" or (not alive and time.time() - t0 > 30) or time.time() - t0 > 1200:
            tail = h.out(f"grep -h ERROR {data}/logs/*.log | head -3; tail -3 {data}/logs/localcluster.log")
            raise SystemExit(f"{role}: localcluster {st.get('state') or 'not running'} on {h}:\n{tail}")
        time.sleep(5)


def merge(roles, opts, st_relays, st_exits, n_relays):
    nodes = []
    for role, st, offset in (("relays", st_relays, 0), ("exits", st_exits, n_relays)):
        r = roles[role]
        for nd in st.get("nodes", []):
            m = dict(nd, id=offset + nd["id"], cluster_id=nd["id"], role=role, ssh=r.get("ssh"))
            # the REST URL as the clients' machine reaches it (the localcluster reports the host it bound)
            port = str(m.get("api_url", "")).rsplit(":", 1)[-1].strip("/")
            m["api_url"] = f"http://{r['addr']}:{port}"
            nodes.append(m)
    return {"state": "running", "multihost": True, "blokli_url": chain_url(roles), "nodes": nodes,
            "extras": st_relays.get("extras", []), "ssh_options": opts,
            "hosts": {r: {"ssh": roles[r].get("ssh"), "addr": roles[r].get("addr")} for r in ROLES}}


def fetch_extras(roles, cfg, extras):
    """The clients' identities, minted by the relays' cluster, into CONFIG_DIR as client.sh expects them."""
    h = roles["relays"]["host"]
    for ex in extras:
        i = ex["id"]
        (cfg.config_dir / f"extra_id_{i}.id").write_text(h.out(f"cat {shlex.quote(ex['keystore_path'])}"))
        (cfg.config_dir / f"extra_id_{i}.password").write_text(ex["password"] + "\n")
        (cfg.config_dir / f"extra_id_{i}.safe").write_text(ex["safe_address"] + "\n")
        (cfg.config_dir / f"extra_id_{i}.module").write_text(ex["module_address"] + "\n")
        if i == 0:
            for ext in ("id", "password", "safe", "module"):
                (cfg.config_dir / f"extra_id.{ext}").write_text((cfg.config_dir / f"extra_id_0.{ext}").read_text())


def exit_services(roles, servers, mode_env):
    """VPN servers and the traffic target on the exits' machine (its repo's recipes); returns the target's address."""
    r = roles["exits"]
    h, repo = r["host"], r["repo"]
    env = f"SERVER_COUNT={servers} SERVER_IMAGE={r.get('server_image', 'gnosis_vpn-server')} {mode_env}"
    h.run(f"cd {repo} && SERVER_COUNT=8 just server-stop target-stop >/dev/null 2>&1; true", timeout=300)
    res = h.run(f"cd {repo} && {env} just server-start target-start", timeout=900)
    if res.returncode:
        raise SystemExit(f"servers/target on {h} failed: {(res.stdout + res.stderr).strip()[-400:]}")
    ip = h.out("docker inspect gnosis_vpn-target --format '{{(index .NetworkSettings.Networks \"gnosis-vpn-target\").IPAddress}}'")
    say(f"exits' machine: {servers} VPN server(s), target {ip}")
    return ip


def standard_config(cfg, merged):
    """client.toml as `just gen-config` writes it, one destination per node (node-<global id>)."""
    t = REPO / "templates"
    dests = "".join(string.Template((t / "destination.toml.tpl").read_text()).substitute(
        DEST_ID=str(nd["id"]), DEST_ADDRESS=nd["address"], DEST_HOPS="1") + "\n" for nd in merged["nodes"])
    text = string.Template((t / "client.toml.tpl").read_text()).safe_substitute(
        DESTINATIONS=dests, PIX_SECTION=(t / "pix-off.toml.tpl").read_text())
    (cfg.config_dir / "client.toml").write_text(text)


def full_mesh(cluster, merged, amount, timeout):
    ids = [nd["id"] for nd in merged["nodes"]]
    addr = {nd["id"]: nd["address"] for nd in merged["nodes"]}
    t0 = time.time()
    while True:
        missing = [(a, b) for a in ids for b in ids if a != b and not any(
            str(x.get("peerAddress", "")).lower() == addr[b].lower() for x in cluster.open_outgoing(a))]
        if not missing:
            say(f"full mesh: {len(ids) * (len(ids) - 1)} channels Open")
            return
        if time.time() - t0 > timeout:
            raise SystemExit(f"full mesh not open after {timeout}s: {missing}")
        for a, b in missing:
            cluster.api(a, "POST", "/api/v4/channels", {"destination": addr[b], "amount": amount}, timeout=120)
        time.sleep(10)


def down(args, opts=None, roles=None):
    if roles is None:
        opts, roles = load_hosts(args.hosts)
    env = dict(os.environ)
    just(dict(env, SERVER_COUNT="8"), "client-stop", "clients-stop", timeout=600)
    state = env.get("CLIENT_STATE_DIR") or "/tmp/gnosis_vpn-testenv-state"
    for k in range(1, 17):
        d = state if k == 1 else f"{state}-{k}"
        roles["clients"]["host"].run(f"rm -rf {shlex.quote(d)}", timeout=60)
    ex = roles["exits"]
    ex["host"].run(f"cd {ex['repo']} && SERVER_COUNT=8 just server-stop target-stop >/dev/null 2>&1; true", timeout=300)
    for role in ("exits", "relays"):
        stop_cluster(roles, role)
    roles["chain"]["host"].run(f"docker rm -f {CHAIN_NAME} >/dev/null 2>&1; true", timeout=120)
    cfg = Config()
    for f in ("multihost.json", "multihost.env", relaytopo.TOPOLOGY_FILE):
        (cfg.config_dir / f).unlink(missing_ok=True)
    say("multihost stack down")


def up(args):
    opts, roles = load_hosts(args.hosts)
    lay = layout(args.mode, args.n)
    n_relays, n_exits, n = len(lay["relays"]), len(lay["exits"]), lay["n"]
    down(args, opts, roles)
    cfg = Config()
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    start_chain(roles)
    token = secrets.token_hex(16)
    # hoprd is ready (/readyz) only with a peer, so a one-node cluster started alone never gets there (T34's single
    # relay did not): the larger cluster starts first and the smaller one finds its peers on chain. One at a time,
    # since both fund from the chain's one dev account.
    if n_relays == 1 and n_exits == 1:
        raise SystemExit("one relay and one exit: neither cluster can become ready alone; use at least two nodes on one side")
    order = [("relays", n_relays, n), ("exits", n_exits, 0)]
    order.sort(key=lambda x: -x[1])
    st = {role: start_cluster(roles, role, size, extras, token) for role, size, extras in order}
    st_r, st_e = st["relays"], st["exits"]
    merged = merge(roles, opts, st_r, st_e, n_relays)
    status_file = cfg.config_dir / "multihost.json"
    status_file.write_text(json.dumps(merged, indent=2) + "\n")
    (cfg.config_dir / "blokli_url").write_text(merged["blokli_url"] + "\n")
    fetch_extras(roles, cfg, merged["extras"])
    target_ip = exit_services(roles, n if args.mode != "standard" else 1, "")
    env = dict(os.environ, MULTIHOST_STATUS=str(status_file), MULTIHOST_SSH_OPTS=opts, CLIENT_COUNT=str(n),
               CLUSTER_SIZE=str(n_relays + n_exits))
    cluster = Cluster(Config(env))
    state = env.get("CLIENT_STATE_DIR") or "/tmp/gnosis_vpn-testenv-state"
    dest = f"node-{lay['exits'][0]}"
    if args.mode == "standard":
        full_mesh(cluster, merged, args.funding, args.timeout)
        standard_config(cfg, merged)
        for k in range(1, n + 1):
            just(env, "client-start-one", relaytopo.client_name(k), state if k == 1 else f"{state}-{k}", str(k - 1), "client.toml", timeout=600)
    else:
        topo = relaytopo.with_addresses(lay, merged)
        topo.update(created=time.strftime("%FT%TZ", time.gmtime()), exit_channel_funding=args.funding,
                    client_image=env.get("CLIENT_IMAGE", ""), hoprd_bin=roles["relays"]["hoprd_bin"], multihost=merged["hosts"])
        for c in topo["clients"]:
            (cfg.config_dir / c["config"]).write_text(relaytopo.render_client_config(REPO / "templates", c))
        relaytopo.save(cfg.config_dir, topo)
        open_exit_channels(cluster, topo, args.funding, args.timeout)
        for c in topo["clients"]:
            just(env, "client-start-one", c["name"], state if c["k"] == 1 else f"{state}-{c['k']}", str(c["extra"]), c["config"], timeout=600)
        say("waiting for every client's channel to its relay")
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
        say(f"topology reached after {int(time.time() - t0)}s: " + "; ".join(f"{k}: {v}" for k, v in summary.items()))
        topo["ready"] = time.strftime("%FT%TZ", time.gmtime())
        relaytopo.save(cfg.config_dir, topo)
    (cfg.config_dir / "multihost.env").write_text(
        f"export MULTIHOST_STATUS={shlex.quote(str(status_file))} MULTIHOST_SSH_OPTS={shlex.quote(opts)} "
        f"TARGET_HOST={target_ip} DEST={dest} CLUSTER_SIZE={n_relays + n_exits} CLIENT_COUNT={n}\n")
    say(f"multihost {args.mode} n={n} up: relays {[f'node-{i}' for i in lay['relays']]} on {roles['relays']['host']}, "
        f"exits {[f'node-{i}' for i in lay['exits']]} on {roles['exits']['host']}, chain on {roles['chain']['host']}, "
        f"target {target_ip}; env in {cfg.config_dir / 'multihost.env'}")


def check(args):
    """Every machine: reachable, the binaries and images its roles need, their versions and checksums."""
    opts, roles = load_hosts(args.hosts)
    bad = 0
    for role, r in roles.items():
        h = r["host"]
        need = {"chain": ["docker"], "relays": [r.get("hoprd_bin"), r.get("localcluster_bin")],
                "exits": [r.get("hoprd_bin"), r.get("localcluster_bin"), "docker", "just", "wg", "envsubst"],
                "clients": ["docker", "just", "curl"]}[role]
        cmds = "; ".join(f"command -v {shlex.quote(str(x))} >/dev/null 2>&1 && echo 'ok {x}' || echo 'MISSING {x}'" for x in need if x)
        out = h.out(f"hostname; nproc; {cmds}", timeout=60, default="UNREACHABLE")
        imgs = {"chain": [r.get("chain_image", "europe-west3-docker.pkg.dev/hoprassociation/docker-images/bloklid-anvil:latest")],
                "exits": [r.get("server_image", "gnosis_vpn-server"), "gnosis_vpn-target"],
                "clients": [os.environ.get("CLIENT_IMAGE", "gnosis_vpn-client"), "gnosis_vpn-suite-tools"]}.get(role, [])
        for img in imgs:
            out += "\n" + ("ok " if h.ok(f"docker image inspect {shlex.quote(img)} >/dev/null 2>&1") else "MISSING image ") + img
        if role in ("relays", "exits"):
            out += "\n" + h.out(f"{r['hoprd_bin']} --version; sha256sum {r['hoprd_bin']} {r['localcluster_bin']} | cut -c1-16")
        if role == "exits":
            out += "\n" + ("ok repo " if h.ok(f"test -f {r['repo']}/justfile") else "MISSING repo ") + str(r.get("repo"))
        bad += out.count("MISSING") + out.count("UNREACHABLE")
        print(f"== {role} on {h} (addr {r.get('addr', 'local')})\n{out}")
    raise SystemExit(1 if bad else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hosts", help="the hosts file (TOML, see multihost/hosts.example.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up", help="take the multihost stack down and bring a topology up")
    u.add_argument("mode", choices=MODES)
    u.add_argument("n", type=int)
    u.add_argument("--funding", default=os.environ.get("RELAY_TOPO_FUNDING", "1 wxHOPR"))
    u.add_argument("--timeout", type=int, default=900)
    sub.add_parser("down", help="stop everything on every machine")
    sub.add_parser("check", help="what each machine has")
    args = ap.parse_args()
    {"up": up, "down": down, "check": check}[args.cmd](args)


if __name__ == "__main__":
    main()
