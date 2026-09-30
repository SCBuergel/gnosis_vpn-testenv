#!/usr/bin/env python3
"""The testenv across several machines: chain, relays, exits and clients on machines of their own, any of them
sharing one, or one machine per node and per client (a DigitalOcean fleet, multihost/do_fleet.py). Run on the
runner: the machine the suite runs on (it reaches every other machine over ssh, and each client's docker over ssh).

    just multihost-check HOSTS                # what each machine has: binaries, images, repo, reachability
    just multihost-provision HOSTS            # copy what is missing from this machine (binaries, images, repo)
    just multihost-up HOSTS paired 5          # T33-relay-baseline's topology across the machines
    just multihost-up HOSTS shared 5          # T34-single-relay-scaling's
    just multihost-up HOSTS standard 5        # T22-concurrent-clients': 2 relays, 1 exit, full mesh, 5 clients
    just multihost-test t33                   # the test against it (sources CONFIG_DIR/multihost.env)
    just multihost-down HOSTS

HOSTS is a TOML file (multihost/hosts.example.toml). Per role either one machine (`ssh` + `addr`) or a list
(`machines = [{ ssh, addr }, ...]`); `ssh = "local"` is the runner itself. `addr` is where the other machines and the
client containers reach it; give private-network addresses, everything binds to them.

How a stack comes up. The chain container (Anvil + Blokli) starts on the chain machine, published on its `addr`. Each
relay and exit machine runs a hoprd-localcluster with `--chain-url` at it, `--p2p-host`/`--api-host` its own `addr`
and `--channel-management none`; the nodes of a role are spread over its machines. The clusters start one after the
other, each once the one before has spawned its nodes: they fund from the chain's one dev account (so never at the
same time), and a node is ready (/readyz) only with a peer (so none may wait for readiness alone). Each localcluster
pre-announces its nodes right after their Safes, so every node reaches every other through Blokli. The first relay
cluster also mints the clients' identities. Each exit machine runs the VPN servers of the clients whose exit it holds
and a traffic target (the same address on each, 198.18.0.2). The merged status (every node with a global id: relays
0.., exits after them; every client with its machine) lands in CONFIG_DIR/multihost.json; the clients start on their
machines; the topology's channels are opened:

- paired / shared: tests/suitelib/relaytopo.py (one channel per client and per exit, to the assigned relay);
- standard: every node opens a channel to every other node, the clients use their own strategy.

CONFIG_DIR/multihost.env holds what the suite needs: MULTIHOST_STATUS, MULTIHOST_SSH_OPTS, TARGET_HOST, DEST,
CLUSTER_SIZE. The suite reaches a remote client's docker as ssh://<its ssh target> (a block in ~/.ssh/config on the
runner, written here, gives docker the key and a shared connection)."""
import argparse
import json
import os
import re
import secrets
import shlex
import string
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relay_topology import full_topology, just, open_exit_channels, say  # noqa: E402
from suitelib import relaytopo, shell  # noqa: E402
from suitelib.cluster import Cluster  # noqa: E402
from suitelib.config import Config  # noqa: E402
from suitelib.hosts import Host  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ROLES = ("chain", "relays", "exits", "clients")
CHAIN_PORT = 8080
CHAIN_IMAGE = "europe-west3-docker.pkg.dev/hoprassociation/docker-images/bloklid-anvil:latest"
# API and P2P port bases per node role, distinct so relays and exits can share a machine
PORTS = {"relays": (3000, 9000), "exits": (3100, 9100)}
DATA = {"relays": "/tmp/hopr-mh-relays", "exits": "/tmp/hopr-mh-exits"}
CHAIN_NAME = "hopr-chain"
MODES = ("paired", "shared", "single-exit", "standard")
REMOTE_CONFIG_DIR = "/tmp/gnosis_vpn-testenv"
SSH_BLOCK = ("# >>> gnosis_vpn-testenv multihost (written by tests/multihost.py)", "# <<< gnosis_vpn-testenv multihost")


# -- hosts file ----------------------------------------------------------------------------------------------------
def load_hosts(path):
    d = tomllib.loads(Path(path).read_text())
    opts = d.get("ssh", {}).get("options", "")
    roles = {}
    for r in ROLES:
        if r not in d.get("roles", {}):
            raise SystemExit(f"{path}: [roles.{r}] missing")
        x = dict(d["roles"][r])
        ms = x.pop("machines", None) or [{"ssh": x.get("ssh", "local"), "addr": x.get("addr", "")}]
        x["machines"] = []
        for i, m in enumerate(ms):
            m = dict(m)
            m["host"] = Host(m.get("ssh"), opts)
            m.setdefault("name", f"{r}-{i + 1}")
            if r != "clients" and not m.get("addr"):
                raise SystemExit(f"{path}: roles.{r} machine {m['name']}: addr missing (the address the others reach it at)")
            x["machines"].append(m)
        if r == "chain" and len(x["machines"]) != 1:
            raise SystemExit(f"{path}: one chain machine")
        roles[r] = x
    return opts, roles


def chain(roles):
    return roles["chain"]["machines"][0]


def chain_url(roles):
    return f"http://{chain(roles)['addr']}:{CHAIN_PORT}"


def ssh_key(opts):
    a = shlex.split(opts or "")
    return a[a.index("-i") + 1] if "-i" in a else ""


def write_ssh_config(opts, roles):
    """docker -H ssh://... cannot take ssh options; a block in ~/.ssh/config gives it the key and one shared
    connection per machine (every suite call to a remote client is a docker command)."""
    remote = [m["ssh"].split("@")[-1] for m in roles["clients"]["machines"] if not m["host"].local]
    cfgp = Path.home() / ".ssh" / "config"
    old = cfgp.read_text() if cfgp.exists() else ""
    old = re.sub(re.escape(SSH_BLOCK[0]) + r".*?" + re.escape(SSH_BLOCK[1]) + r"\n?", "", old, flags=re.S)
    if not remote:
        if cfgp.exists():
            cfgp.write_text(old)
        return
    key = ssh_key(opts)
    block = (f"{SSH_BLOCK[0]}\nHost {' '.join(remote)}\n  User root\n" + (f"  IdentityFile {key}\n  IdentitiesOnly yes\n" if key else "")
             + "  StrictHostKeyChecking accept-new\n  UserKnownHostsFile ~/.ssh/fleet_known_hosts\n"
             "  ControlMaster auto\n  ControlPath /tmp/gvpn-ssh-%r@%h:%p\n  ControlPersist 60m\n  ServerAliveInterval 15\n"
             f"{SSH_BLOCK[1]}\n")
    cfgp.parent.mkdir(mode=0o700, exist_ok=True)
    cfgp.write_text(old.rstrip("\n") + ("\n\n" if old.strip() else "") + block)
    cfgp.chmod(0o600)


def spread(size, machines):
    """size nodes over the machines, as evenly as the order allows: [(machine, count), ...] without empty ones."""
    n = len(machines)
    out = []
    for i, m in enumerate(machines):
        k = size // n + (1 if i < size % n else 0)
        if k:
            out.append((m, k))
    return out


def layout(mode, n):
    if mode in relaytopo.MODES:
        return relaytopo.layout(mode, n)
    # standard: T22-concurrent-clients' stack, two relays and one exit in a full mesh, n clients on their own strategy
    return {"mode": "standard", "n": int(n), "cluster_size": 3, "relays": [0, 1], "exits": [2], "clients": []}


# -- chain -----------------------------------------------------------------------------------------------------------
def start_chain(roles):
    c = chain(roles)
    h, image = c["host"], roles["chain"].get("chain_image", CHAIN_IMAGE)
    h.run(f"docker rm -f {CHAIN_NAME} >/dev/null 2>&1; for i in $(seq 1 30); do docker container inspect {CHAIN_NAME} >/dev/null 2>&1 || break; sleep 1; done")
    r = h.run(f"docker run -d --rm --name {CHAIN_NAME} --platform linux/amd64 -p {c['addr']}:{CHAIN_PORT}:{CHAIN_PORT} {image}", timeout=300)
    if r.returncode:
        raise SystemExit(f"chain container did not start on {h}: {r.stderr.strip()[:300]}")
    t0 = time.time()
    while Host(None).out(f"curl -s -o /dev/null -m 5 -w '%{{http_code}}' {chain_url(roles)}/", timeout=20) in ("", "000"):
        if time.time() - t0 > 300:
            raise SystemExit(f"Blokli at {chain_url(roles)} not answering after 300 s")
        time.sleep(3)
    say(f"chain on {h} at {chain_url(roles)}")


# -- localclusters ---------------------------------------------------------------------------------------------------
def stop_cluster(roles, rname, m):
    r = roles[rname]
    lc, hoprd, data = r["localcluster_bin"], r["hoprd_bin"], DATA[rname]
    # anchored at the binaries' paths: the ssh shell running this line starts with "sh"/"bash", so it never matches itself
    m["host"].run(f"pkill -TERM -f '^{lc} .*--data-dir {data}' 2>/dev/null; "
                  f"for i in $(seq 1 30); do pgrep -f '^{lc} .*--data-dir {data}' >/dev/null || break; sleep 1; done; "
                  f"pkill -KILL -f '^{lc} .*--data-dir {data}' 2>/dev/null; pkill -KILL -f '^{hoprd} .*{data}' 2>/dev/null; rm -rf {data}; true",
                  timeout=120)


def launch_cluster(roles, rname, m, size, extras, token):
    r = roles[rname]
    h, lc, data = m["host"], r["localcluster_bin"], DATA[rname]
    api, p2p = PORTS[rname]
    args = (f"--hoprd-bin {r['hoprd_bin']} --chain-url {chain_url(roles)} --size {size} --p2p-host {m['addr']} "
            f"--p2p-port-base {p2p} --api-host {m['addr']} --api-port-base {api} --api-token {token} --data-dir {data} "
            f"--channel-management none --funding-amount '1 wxHOPR' --extra-identities {extras}")
    # the background job alone redirected, not an `a && b && c &` list: a backgrounded list keeps the ssh session's
    # stdout open and the call returned only at its timeout
    h.run(f"rm -rf {data}; mkdir -p {data}/logs; cd /tmp; setsid nohup env RUST_LOG=info {lc} {args} "
          f"> {data}/logs/localcluster.log 2>&1 < /dev/null & echo started", timeout=60)
    say(f"{rname}: localcluster of {size} node(s) on {m['name']} {h} (p2p {m['addr']}:{p2p}+, api :{api}+)")


def wait_cluster(roles, rname, m, until, timeout=1200):
    """Poll one machine's localcluster until `until` ("spawned": its nodes are started, the funding and announcing are
    done; "running": ready). Fails at once when the localcluster is gone."""
    r = roles[rname]
    h, lc, data = m["host"], r["localcluster_bin"], DATA[rname]
    t0 = time.time()
    while True:
        try:
            st = json.loads(h.out(f"{lc} status --data-dir {data}", timeout=60))
        except ValueError:
            st = {}
        state = st.get("state")
        if state == "running":
            return st
        if until == "spawned" and h.ok(f"grep -q 'waiting for nodes to be ready' {data}/logs/localcluster.log", timeout=30):
            return st
        alive = h.ok(f"pgrep -f '^{lc} .*--data-dir {data}' >/dev/null", timeout=30)
        if state == "failed" or (not alive and time.time() - t0 > 30) or time.time() - t0 > timeout:
            tail = h.out(f"grep -h ERROR {data}/logs/*.log | grep -v opentelemetry | head -3; tail -3 {data}/logs/localcluster.log")
            raise SystemExit(f"{rname} on {m['name']}: localcluster {state or 'not running'}:\n{tail}")
        time.sleep(5)


def start_clusters(roles, n_relays, n_exits, n_extras, token):
    """Every relay and exit machine's localcluster, one after the other (see the module docstring), then all ready.
    Returns [(role, machine, status)] in global id order: relays first."""
    plan = [("relays", m, k) for m, k in spread(n_relays, roles["relays"]["machines"])] + \
           [("exits", m, k) for m, k in spread(n_exits, roles["exits"]["machines"])]
    # the clients' identities: a localcluster mints at most 5, so they are spread over the clusters in plan order
    extras, left = [], n_extras
    for _ in plan:
        extras.append(min(5, left))
        left -= extras[-1]
    if left:
        raise SystemExit(f"{n_extras} client identities need at least {-(-n_extras // 5)} node machines (5 per localcluster)")
    if len(plan) == 2 and plan[0][2] == plan[1][2] == 1:
        raise SystemExit("one relay and one exit: neither cluster can become ready alone; use at least three nodes")
    # larger clusters first; a one-node cluster started first becomes ready once the next one's nodes come up
    for i in sorted(range(len(plan)), key=lambda i: -plan[i][2]):
        rname, m, k = plan[i]
        launch_cluster(roles, rname, m, k, extras[i], token)
        wait_cluster(roles, rname, m, "spawned")
    out = []
    for rname, m, k in plan:
        out.append((rname, m, wait_cluster(roles, rname, m, "running")))
    say(f"{len(plan)} localcluster(s) running")
    return out


def merge(roles, opts, started):
    """One status for every machine's cluster: nodes with global ids (relays first), the clients' identities with
    global ids in the same order, each with the machine that holds its keystore."""
    nodes, extras, gid = [], [], 0
    for rname, m, st in started:
        for nd in sorted(st.get("nodes", []), key=lambda x: x["id"]):
            port = str(nd.get("api_url", "")).rsplit(":", 1)[-1].strip("/")
            nodes.append(dict(nd, id=gid, cluster_id=nd["id"], role=rname, ssh=m.get("ssh"), machine=m["name"],
                              api_url=f"http://{m['addr']}:{port}"))
            gid += 1
        for ex in sorted(st.get("extras") or [], key=lambda x: x["id"]):
            extras.append(dict(ex, id=len(extras), cluster_id=ex["id"], ssh=m.get("ssh")))
    # two clients with one identity share a peer id: the network delivers one client's return traffic to the other.
    # An unpatched localcluster mints its extras from five frozen secrets, so every cluster minted the same five.
    seen = {}
    for ex in extras:
        a = str(ex.get("address", "")).lower()
        if a and a in seen:
            raise SystemExit(f"client identities {seen[a]} and {ex['id']} are the same ({a}): build the localcluster with "
                             f"patches/hoprd-localcluster-max16.patch (random extras), or use at most 5 clients")
        seen[a] = ex["id"]
    return {"state": "running", "multihost": True, "blokli_url": chain_url(roles), "nodes": nodes, "extras": extras,
            "extras_ssh": extras[0]["ssh"] if extras else None, "ssh_options": opts, "clients": {},
            "machines": {r: [{"name": m["name"], "ssh": m.get("ssh"), "addr": m.get("addr")} for m in roles[r]["machines"]] for r in ROLES}}


def fetch_extras(opts, merged, cfg):
    """The clients' identities, minted by the clusters, into the runner's CONFIG_DIR as client.sh expects."""
    for ex in merged["extras"]:
        i = ex["id"]
        h = Host(ex.get("ssh"), opts)
        (cfg.config_dir / f"extra_id_{i}.id").write_text(h.out(f"cat {shlex.quote(ex['keystore_path'])}"))
        (cfg.config_dir / f"extra_id_{i}.password").write_text(ex["password"] + "\n")
        (cfg.config_dir / f"extra_id_{i}.safe").write_text(ex["safe_address"] + "\n")
        (cfg.config_dir / f"extra_id_{i}.module").write_text(ex["module_address"] + "\n")


# -- exit services ---------------------------------------------------------------------------------------------------
def exit_services(roles, servers_by_machine):
    """VPN servers and the traffic target on every exit machine that serves a client; returns the target's address,
    the same on each (198.18.0.2, the first container on a fresh target network)."""
    r = roles["exits"]

    def one(m):
        n = servers_by_machine.get(m["name"], 0)
        if not n:
            return None
        h = m["host"]
        env = f"SERVER_COUNT={n} SERVER_IMAGE={r.get('server_image', 'gnosis_vpn-server')}"
        h.run(f"cd {r['repo']} && SERVER_COUNT=8 just server-stop target-stop >/dev/null 2>&1; true", timeout=300)
        res = h.run(f"cd {r['repo']} && {env} just server-start target-start", timeout=900)
        if res.returncode:
            raise SystemExit(f"servers/target on {m['name']} failed: {(res.stdout + res.stderr).strip()[-400:]}")
        return h.out("docker inspect gnosis_vpn-target --format '{{(index .NetworkSettings.Networks \"gnosis-vpn-target\").IPAddress}}'")

    with ThreadPoolExecutor(8) as ex:
        ips = {ip for ip in ex.map(one, r["machines"]) if ip is not None}
    if len(ips) != 1:
        raise SystemExit(f"the exit machines' targets have different addresses {sorted(ips)}; the suite reaches one TARGET_HOST")
    ip = ips.pop()
    say(f"exit machines: servers {servers_by_machine}, target {ip}")
    return ip


# -- clients ---------------------------------------------------------------------------------------------------------
def client_machine(roles, k):
    ms = roles["clients"]["machines"]
    return ms[(k - 1) % len(ms)]


def start_client(roles, cfg, env, name, k, extra, config_file):
    m = client_machine(roles, k)
    state = env.get("CLIENT_STATE_DIR") or "/tmp/gnosis_vpn-testenv-state"
    sd = state if k == 1 else f"{state}-{k}"
    if m["host"].local:
        just(env, "client-start-one", name, sd, str(extra), config_file, timeout=600)
        return m
    h, repo = m["host"], roles["clients"]["repo"]
    h.run(f"mkdir -p {REMOTE_CONFIG_DIR}", timeout=60)
    for f in ("blokli_url", f"extra_id_{extra}.id", f"extra_id_{extra}.password", config_file):
        r = h.run(f"cat > {REMOTE_CONFIG_DIR}/{f}", input=(cfg.config_dir / f).read_text(), timeout=60)
        if r.returncode:
            raise SystemExit(f"copying {f} to {m['name']} failed: {r.stderr.strip()[:200]}")
    keep = " ".join(f"{v}={shlex.quote(env[v])}" for v in ("CLIENT_IMAGE", "CLIENT_LOG_LEVEL", "CLIENT_AUTOSTART", "CLIENT_EXTRA_ENV",
                                                           "CLIENT_EXTRA_ARGS", "SUITE_OUT_DIR") if env.get(v))
    r = h.run(f"cd {repo} && CONFIG_DIR={REMOTE_CONFIG_DIR} {keep} just client-start-one {name} {sd} {extra} {config_file}", timeout=600)
    if r.returncode:
        raise SystemExit(f"{name} on {m['name']}: {(r.stdout + r.stderr).strip()[-400:]}")
    say(f"{name} started on {m['name']}")
    return m


def stop_clients(roles, env):
    def one(m):
        if m["host"].local:
            just(dict(env, SERVER_COUNT="8"), "client-stop", "clients-stop", timeout=600)
            state = env.get("CLIENT_STATE_DIR") or "/tmp/gnosis_vpn-testenv-state"
            m["host"].run(f"rm -rf {shlex.quote(state)} {shlex.quote(state)}-*", timeout=60)
        else:
            m["host"].run(f"cd {roles['clients']['repo']} && just client-stop clients-stop >/dev/null 2>&1; "
                          f"rm -rf /tmp/gnosis_vpn-testenv-state /tmp/gnosis_vpn-testenv-state-*; true", timeout=600)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, roles["clients"]["machines"]))


# -- standard mode ---------------------------------------------------------------------------------------------------
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


# -- commands --------------------------------------------------------------------------------------------------------
def down(args, opts=None, roles=None):
    if roles is None:
        opts, roles = load_hosts(args.hosts)
    env = dict(os.environ)
    stop_clients(roles, env)
    ex = roles["exits"]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda m: m["host"].run(f"cd {ex['repo']} && SERVER_COUNT=8 just server-stop target-stop >/dev/null 2>&1; true",
                                              timeout=300), ex["machines"]))
        list(pool.map(lambda rm: stop_cluster(roles, rm[0], rm[1]), [(r, m) for r in ("exits", "relays") for m in roles[r]["machines"]]))
    chain(roles)["host"].run(f"docker rm -f {CHAIN_NAME} >/dev/null 2>&1; true", timeout=120)
    cfg = Config()
    for f in ("multihost.json", "multihost.env", relaytopo.TOPOLOGY_FILE):
        (cfg.config_dir / f).unlink(missing_ok=True)
    say("multihost stack down")


def up(args):
    opts, roles = load_hosts(args.hosts)
    lay = layout(args.mode, args.n)
    n_relays, n_exits, n = len(lay["relays"]), len(lay["exits"]), lay["n"]
    down(args, opts, roles)
    write_ssh_config(opts, roles)
    cfg = Config()
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    start_chain(roles)
    merged = merge(roles, opts, start_clusters(roles, n_relays, n_exits, n, secrets.token_hex(16)))
    status_file = cfg.config_dir / "multihost.json"
    (cfg.config_dir / "blokli_url").write_text(merged["blokli_url"] + "\n")
    fetch_extras(opts, merged, cfg)
    machine_of = {nd["id"]: nd["machine"] for nd in merged["nodes"]}
    # the VPN servers: in the relay topologies one per client on its exit's machine, numbered per machine; in standard
    # one server every client shares
    topo = None
    if args.mode == "standard":
        servers = {machine_of[lay["exits"][0]]: 1}
    else:
        topo = relaytopo.with_addresses(lay, merged)
        servers = {}
        for c in topo["clients"]:
            mname = machine_of[c["exit"]]
            c["server"] = servers.get(mname, 0)
            servers[mname] = c["server"] + 1
    target_ip = exit_services(roles, servers)
    for k in range(1, n + 1):
        m = client_machine(roles, k)
        merged["clients"][relaytopo.client_name(k)] = {"ssh": None if m["host"].local else m["ssh"], "machine": m["name"]}
    status_file.write_text(json.dumps(merged, indent=2) + "\n")
    env = dict(os.environ, MULTIHOST_STATUS=str(status_file), MULTIHOST_SSH_OPTS=opts, CLIENT_COUNT=str(n),
               CLUSTER_SIZE=str(n_relays + n_exits))
    cluster = Cluster(Config(env))
    dest = f"node-{lay['exits'][0]}"
    if args.mode == "standard":
        full_mesh(cluster, merged, args.funding, args.timeout)
        standard_config(cfg, merged)
        for k in range(1, n + 1):
            start_client(roles, cfg, env, relaytopo.client_name(k), k, k - 1, "client.toml")
    else:
        topo.update(created=time.strftime("%FT%TZ", time.gmtime()), exit_channel_funding=args.funding,
                    client_image=env.get("CLIENT_IMAGE", ""), hoprd_bin=roles["relays"]["hoprd_bin"], multihost=merged["machines"])
        for c in topo["clients"]:
            (cfg.config_dir / c["config"]).write_text(relaytopo.render_client_config(REPO / "templates", c))
        relaytopo.save(cfg.config_dir, topo)
        open_exit_channels(cluster, topo, args.funding, args.timeout)
        for c in topo["clients"]:
            start_client(roles, cfg, env, c["name"], c["k"], c["extra"], c["config"])
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
    say(f"multihost {args.mode} n={n} up: {n_relays} relay(s) on {len(roles['relays']['machines'])} machine(s), {n_exits} exit(s) "
        f"on {len(roles['exits']['machines'])}, {n} client(s) on {len(roles['clients']['machines'])}; target {target_ip}; "
        f"env in {cfg.config_dir / 'multihost.env'}")


def need(role, r):
    """(binaries, images, repo?) a machine of this role needs."""
    if role == "chain":
        return [], [r.get("chain_image", CHAIN_IMAGE)], False
    if role == "relays":
        return [r["hoprd_bin"], r["localcluster_bin"]], [], False
    if role == "exits":
        return [r["hoprd_bin"], r["localcluster_bin"]], [r.get("server_image", "gnosis_vpn-server"), "gnosis_vpn-target"], True
    return [], [os.environ.get("CLIENT_IMAGE", "gnosis_vpn-client"), "gnosis_vpn-suite-tools"], True


def machine_report(role, r, m):
    h = m["host"]
    bins, imgs, repo = need(role, r)
    lines = [h.out("echo $(hostname) $(nproc)cpu", timeout=60, default="UNREACHABLE")]
    for b in bins:
        lines.append(h.out(f"test -x {b} && echo \"ok {b} $(sha256sum {b} | cut -c1-16)\" || echo 'MISSING {b}'", timeout=60, default=f"MISSING {b}"))
    for i in imgs:
        lines.append(("ok " if h.ok(f"docker image inspect {shlex.quote(i)} >/dev/null 2>&1") else "MISSING image ") + i)
    for tool in (["docker", "just", "wg", "envsubst"] if role in ("exits", "clients") else ["docker"] if role == "chain" else []):
        lines.append(("ok " if h.ok(f"command -v {tool} >/dev/null") else "MISSING ") + tool)
    if repo:
        lines.append(("ok repo " if h.ok(f"test -f {r['repo']}/justfile") else "MISSING repo ") + str(r.get("repo")))
    return lines


def check(args):
    """Every machine: reachable, the binaries and images its role needs, their checksums."""
    opts, roles = load_hosts(args.hosts)
    jobs = [(role, roles[role], m) for role in ROLES for m in roles[role]["machines"]]
    with ThreadPoolExecutor(16) as ex:
        reports = list(ex.map(lambda j: machine_report(*j), jobs))
    bad = 0
    for (role, r, m), lines in zip(jobs, reports):
        bad += sum(1 for x in lines if x.startswith(("MISSING", "UNREACHABLE")))
        print(f"== {role} {m['name']} {m['host']} (addr {m.get('addr') or 'local'}): " + "; ".join(lines))
    raise SystemExit(1 if bad else 0)


def provision(args):
    """Copy what a machine is missing from the runner: binaries (same path, checked by sha256), docker images, the repo
    (git clone of the runner's branch from its origin). The machines run in parallel."""
    opts, roles = load_hosts(args.hosts)
    url = shell.out(["git", "-C", str(REPO), "remote", "get-url", "origin"], timeout=30)
    branch = shell.out(["git", "-C", str(REPO), "rev-parse", "--abbrev-ref", "HEAD"], timeout=30)
    here = Host(None)

    def remote(h, cmd):
        return " ".join(shlex.quote(x) for x in h.argv(cmd))

    def one(job):
        role, r, m = job
        h = m["host"]
        if h.local:
            return f"{m['name']}: this machine, skipped"
        bins, imgs, repo = need(role, r)
        done = []
        for b in bins:
            want = here.out(f"sha256sum {shlex.quote(b)} | cut -d' ' -f1")
            if not want:
                raise SystemExit(f"{b} is not on this machine; provision copies from here")
            if h.out(f"sha256sum {b} 2>/dev/null | cut -d' ' -f1") != want:
                res = shell.run(remote(h, f"mkdir -p {os.path.dirname(b)} && cat > {b} && chmod +x {b}") + f" < {shlex.quote(b)}", timeout=900)
                if res.returncode or h.out(f"sha256sum {b} | cut -d' ' -f1") != want:
                    raise SystemExit(f"{m['name']}: copying {b} failed")
                done.append(os.path.basename(b))
        missing = [i for i in imgs if not h.ok(f"docker image inspect {shlex.quote(i)} >/dev/null 2>&1")]
        if missing:
            res = shell.run(f"docker save {' '.join(shlex.quote(i) for i in missing)} | gzip -1 | " + remote(h, "gunzip | docker load"), timeout=1800)
            if res.returncode:
                raise SystemExit(f"{m['name']}: docker load failed: {res.stderr.strip()[-300:]}")
            done.append(f"{len(missing)} image(s)")
        if repo:
            if not h.ok(f"test -f {r['repo']}/justfile"):
                res = h.run(f"mkdir -p {os.path.dirname(r['repo'])} && git clone -q -b {branch} {url} {r['repo']}", timeout=600)
                if res.returncode:
                    raise SystemExit(f"{m['name']}: git clone failed: {res.stderr.strip()[:300]}")
                done.append("repo")
            else:
                h.run(f"cd {r['repo']} && git fetch -q origin && git checkout -q {branch} && git pull -q --ff-only", timeout=300)
                done.append("repo updated")
        return f"{m['name']}: {', '.join(done) or 'nothing missing'}"

    jobs = [(role, roles[role], m) for role in ROLES for m in roles[role]["machines"]]
    with ThreadPoolExecutor(args.parallel) as ex:
        for line in ex.map(one, jobs):
            say(line)


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
    p = sub.add_parser("provision", help="copy what the machines are missing from this one")
    p.add_argument("--parallel", type=int, default=8)
    args = ap.parse_args()
    {"up": up, "down": down, "check": check, "provision": provision}[args.cmd](args)


if __name__ == "__main__":
    main()
