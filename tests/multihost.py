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
and `--channel-management none`; the nodes of a role are spread over its machines. The chain starts with
FUNDER_ACCOUNTS Anvil accounts, account 0 hands the others wxHOPR, and every cluster funds from one of its own
(--funder-key), LAUNCH_PARALLEL at a time (prepare_funders, funded_launch); a localcluster without the option starts one
after the other, each once the one before has spawned its nodes (one funding account: never at the same time). A node
is ready (/readyz) only with a peer, so none may wait for readiness alone. Each localcluster pre-announces its nodes
right after their Safes, so every node reaches every other through Blokli. The clients' identities are minted spread
over the clusters. The clients start CLIENT_BATCH at a time, each waited for until Ready (start_clients); `doctor`
has every client connect and ping through its tunnel. Each exit machine runs the VPN server of every exit node it holds
and a traffic target (the same address on each, 198.18.0.2). The merged status (every node with a global id: relays
0.., exits after them; every client with its machine) lands in CONFIG_DIR/multihost.json; the clients start on their
machines; the topology's channels are opened:

- paired / shared: tests/suitelib/relaytopo.py (one channel per client and per exit, to the assigned relay);
- standard: every node opens a channel to every other node, the clients use their own strategy.

CONFIG_DIR/multihost.env holds what the suite needs: MULTIHOST_STATUS, MULTIHOST_SSH_OPTS, TARGET_HOST, DEST,
CLUSTER_SIZE. The suite reaches a remote client's docker as ssh://<its ssh target> (a block in ~/.ssh/config on the
runner, written here, gives docker the key and a shared connection)."""
import argparse
import fcntl
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
FUND_LOCK = "/tmp/gnosis_vpn-testenv-funding.lock"   # one localcluster funding from the chain's dev account at a time
# clusters fund from that one account; launched this many seconds apart they fund side by side (0: one after the other).
# 0 by default: on 2026-10-08 (awsl2) 31 clusters launched 1 s apart, 26 died within 36 s on "replacement transaction
# underpriced" from signer 0xf39f...2266 (Anvil account 0, the only funder; hoprd-localcluster has no option for
# another): the localcluster keeps the signer's nonce itself, one foreign transaction breaks its sequence and it quits.
# Parallel funding needs one funding account per cluster, a localcluster change.
# Relaunching the clusters that died is not enough: 12 of the 25 relaunched exits left their client with "cannot find
# 1 hop path ... in the channel graph" for good (none of the 5 that started clean), most likely the dead first
# attempt's on-chain records at the same address; restarting the client cured one of the first five. Start over.
LAUNCH_STAGGER_S = 0
# Parallel funding instead (hoprd-localcluster built with patches/hoprd-localcluster-funder-key.patch): the chain starts
# with FUNDER_ACCOUNTS Anvil dev accounts (Anvil's public test mnemonic), account 0 hands each of 1.. FUNDER_WXHOPR
# wxHOPR, and every cluster funds from an account of its own (--funder-key): no shared nonce, so they launch side by
# side, at most LAUNCH_PARALLEL at once (Blokli confirms every transaction; 30 at once is not what it was sized for).
ANVIL_MNEMONIC = "test test test test test test test test test test test junk"
FUNDER_ACCOUNTS = 64
FUNDER_WXHOPR = 50000
LAUNCH_PARALLEL = 8
CLUSTER_TRIES = 3
# Clients start CLIENT_BATCH at a time; each must reach "Ready" (its node running, its Safe registered) within
# CLIENT_READY_S, else its log is saved and it is started again, CLIENT_TRIES times in all.
CLIENT_BATCH = 4
CLIENT_READY_S = 240
CLIENT_TRIES = 3
CLASH_PATTERN = "nonce too (low|high)|noncetoolow|invalid nonce|replacement transaction|underpriced|already known|insufficient (funds|token balance)"
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


def layout(mode, n, server_per_client=False):
    if mode in relaytopo.MODES:
        return relaytopo.layout(mode, n, server_per_client=server_per_client)
    # standard: T22-concurrent-clients' stack, two relays and one exit in a full mesh, n clients on their own strategy
    return {"mode": "standard", "n": int(n), "cluster_size": 3, "relays": [0, 1], "exits": [2], "clients": []}


# -- chain -----------------------------------------------------------------------------------------------------------
def start_chain(roles):
    c = chain(roles)
    h, image = c["host"], roles["chain"].get("chain_image", CHAIN_IMAGE)
    h.run(f"docker rm -f {CHAIN_NAME} >/dev/null 2>&1; for i in $(seq 1 30); do docker container inspect {CHAIN_NAME} >/dev/null 2>&1 || break; sleep 1; done")
    env = f"-e ANVIL_ACCOUNTS={FUNDER_ACCOUNTS}"
    if os.environ.get("MULTIHOST_BLOCK_TIME"):
        env += f" -e ANVIL_BLOCK_TIME={shlex.quote(os.environ['MULTIHOST_BLOCK_TIME'])}"
    r = h.run(f"docker run -d --rm --name {CHAIN_NAME} --platform linux/amd64 {env} -p {c['addr']}:{CHAIN_PORT}:{CHAIN_PORT} {image}",
              timeout=300)
    if r.returncode:
        raise SystemExit(f"chain container did not start on {h}: {r.stderr.strip()[:300]}")
    t0 = time.time()
    while Host(None).out(f"curl -s -o /dev/null -m 5 -w '%{{http_code}}' {chain_url(roles)}/", timeout=20) in ("", "000"):
        if time.time() - t0 > 300:
            raise SystemExit(f"Blokli at {chain_url(roles)} not answering after 300 s")
        time.sleep(3)
    say(f"chain on {h} at {chain_url(roles)}")


def funders_file():
    return Config().config_dir / "funders.json"


def prepare_funders(roles):
    """Anvil accounts 1..FUNDER_ACCOUNTS-1 as funders, each handed FUNDER_WXHOPR wxHOPR by account 0 (the deployer,
    which holds the supply); their keys (Anvil's public dev keys) in CONFIG_DIR/funders.json, {index: key}. Runs in
    the chain container (cast, Anvil's RPC on its loopback). Without wxHOPR on account 0 or without cast: none, and the
    clusters fund one after the other from account 0 as before."""
    h = chain(roles)["host"]
    n = FUNDER_ACCOUNTS - 1
    # the image has cast, cut and seq, but no grep, sed or awk: the token's address is read here
    tok = re.search(r'^token\s*=\s*"(0x[0-9a-fA-F]{40})"', h.out(f"docker exec {CHAIN_NAME} cat /config.toml", timeout=60), re.M)
    if not tok:
        say("funders not prepared: no token address in the chain's /config.toml; clusters fund from account 0, one after the other")
        return {}
    script = f"""set -e
export FOUNDRY_DISABLE_NIGHTLY_WARNING=1
M={shlex.quote(ANVIL_MNEMONIC)}; RPC=http://127.0.0.1:8545; TOKEN={tok.group(1)}
K0=$(cast wallet private-key --mnemonic "$M" --mnemonic-index 0)
A0=$(cast wallet address --private-key $K0)
echo "token $TOKEN deployer $A0 balance $(cast call --rpc-url $RPC $TOKEN 'balanceOf(address)(uint256)' $A0 | cut -d' ' -f1)"
N=$(cast nonce --rpc-url $RPC $A0)
for i in $(seq 1 {n}); do
  k=$(cast wallet private-key --mnemonic "$M" --mnemonic-index $i); a=$(cast wallet address --private-key $k)
  # explicit consecutive nonces: --async alone reused one and the second transfer was "replacement transaction underpriced"
  cast send --async --nonce $N --rpc-url $RPC --private-key $K0 $TOKEN 'transfer(address,uint256)' $a {FUNDER_WXHOPR}ether >/dev/null
  N=$((N + 1))
  echo "funder $i $k $a"
done
for t in $(seq 1 60); do
  b=$(cast call --rpc-url $RPC $TOKEN 'balanceOf(address)(uint256)' $a | cut -d' ' -f1)
  [ "$b" != "0" ] && break; sleep 1
done
echo "last funder balance $b"
"""
    t0 = time.time()
    r = h.run(f"docker exec -i {CHAIN_NAME} sh -s", input=script, timeout=600)
    funders = {}
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) == 4 and p[0] == "funder":
            funders[int(p[1])] = p[2]
    last = [l for l in r.stdout.splitlines() if l.startswith("last funder balance")]
    if r.returncode or len(funders) != n or not last or last[0].endswith(" 0"):
        say(f"funders not prepared ({len(funders)} of {n}; {(r.stdout + r.stderr).strip()[-300:]}); clusters fund from account 0, one after the other")
        funders_file().unlink(missing_ok=True)
        return {}
    funders_file().write_text(json.dumps(funders) + "\n")
    head = r.stdout.splitlines()[0] if r.stdout else ""
    say(f"{n} funder accounts with {FUNDER_WXHOPR} wxHOPR each in {round(time.time() - t0)}s ({head})")
    return funders


def load_funders():
    try:
        return {int(k): v for k, v in json.loads(funders_file().read_text()).items()}
    except (OSError, ValueError):
        return {}


def supports_funder_key(roles, rnames):
    """Whether every machine of these roles has a localcluster with --funder-key."""
    ms = [(roles[r]["localcluster_bin"], m) for r in rnames for m in roles[r]["machines"]]
    with ThreadPoolExecutor(16) as ex:
        return all(ex.map(lambda x: x[1]["host"].ok(f"{x[0]} --help 2>&1 | grep -q -- --funder-key", timeout=60), ms))


# -- localclusters ---------------------------------------------------------------------------------------------------
def stop_cluster(roles, rname, m):
    r = roles[rname]
    lc, hoprd, data = r["localcluster_bin"], r["hoprd_bin"], DATA[rname]
    # anchored at the binaries' paths: the ssh shell running this line starts with "sh"/"bash", so it never matches itself
    m["host"].run(f"pkill -TERM -f '^{lc} .*--data-dir {data}' 2>/dev/null; "
                  f"for i in $(seq 1 30); do pgrep -f '^{lc} .*--data-dir {data}' >/dev/null || break; sleep 1; done; "
                  f"pkill -KILL -f '^{lc} .*--data-dir {data}' 2>/dev/null; pkill -KILL -f '^{hoprd} .*{data}' 2>/dev/null; rm -rf {data}; true",
                  timeout=120)


def launch_cluster(roles, rname, m, size, extras, token, funder_key=None):
    r = roles[rname]
    h, lc, data = m["host"], r["localcluster_bin"], DATA[rname]
    api, p2p = PORTS[rname]
    args = (f"--hoprd-bin {r['hoprd_bin']} --chain-url {chain_url(roles)} --size {size} --p2p-host {m['addr']} "
            f"--p2p-port-base {p2p} --api-host {m['addr']} --api-port-base {api} --api-token {token} --data-dir {data} "
            f"--channel-management none --funding-amount '1 wxHOPR' --extra-identities {extras}")
    # the background job alone redirected, not an `a && b && c &` list: a backgrounded list keeps the ssh session's
    # stdout open and the call returned only at its timeout
    fund = f"HOPRD_LOCALCLUSTER_FUNDER_KEY={funder_key} " if funder_key else ""
    h.run(f"rm -rf {data}; mkdir -p {data}/logs; cd /tmp; setsid nohup env RUST_LOG=info {fund}{lc} {args} "
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


@__import__("contextlib").contextmanager
def funding_lock():
    """Held from a localcluster's launch until its nodes are funded: `relay-start` may run next to an `up`."""
    with open(FUND_LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def funding_clashes(roles, rname, m):
    """Lines of one cluster's logs that show its funding transactions colliding with another cluster's (they all
    send from the chain's one dev account): nonce errors, a replaced or duplicate transaction, an empty signer."""
    data = DATA[rname]
    return m["host"].out(f"grep -ihE '{CLASH_PATTERN}' {data}/logs/*.log 2>/dev/null | grep -v opentelemetry | head -5; true",
                         timeout=60).strip()


def staggered_launch(roles, plan, order, extras, token, stagger):
    """Every cluster of `order` launched `stagger` s after the one before, each then waited for until spawned (funded
    and announced), so their funding overlaps instead of queueing (30 exits one at a time took ~15 min). The funding
    lock is held for the whole batch: a `relay-start` still never funds inside it. A cluster whose logs show a clash
    or that fails is said, stopped, and launched again alone once the batch is through."""
    def one(j, i):
        rname, m, k = plan[i]
        time.sleep(j * stagger)
        t0 = time.time()
        launch_cluster(roles, rname, m, k, extras[i], token)
        try:
            wait_cluster(roles, rname, m, "spawned")
            err = None
        except SystemExit as e:
            err = str(e)
        return i, round(time.time() - t0), funding_clashes(roles, rname, m), err

    with funding_lock():
        t0 = time.time()
        with ThreadPoolExecutor(min(32, len(order)) or 1) as ex:
            res = list(ex.map(lambda ji: one(*ji), enumerate(order)))
        bad = []
        for i, secs, clash, err in res:
            rname, m, _ = plan[i]
            if clash or err:
                bad.append(i)
                say(f"{rname} on {m['name']}: FUNDING CLASH or failure after {secs}s: " + "; ".join(x for x in (err, clash) if x)[:600])
        say(f"{len(order)} localcluster(s) launched {stagger}s apart, spawned within {round(time.time() - t0)}s; "
            f"{len(bad)} with a clash or failure" + (f" ({', '.join(plan[i][1]['name'] for i in bad)}), relaunched one at a time" if bad else ""))
        for i in bad:
            rname, m, k = plan[i]
            stop_cluster(roles, rname, m)
            launch_cluster(roles, rname, m, k, extras[i], token)
            wait_cluster(roles, rname, m, "spawned")
            clash = funding_clashes(roles, rname, m)
            say(f"{rname} on {m['name']}: relaunched alone" + (f"; clash lines again: {clash[:300]}" if clash else ", clean"))


def funded_launch(roles, plan, order, extras, token, funders, parallel):
    """Every cluster of `order` funding from an account of its own (funders, index 1..), at most `parallel` at a time,
    each waited for until spawned; a cluster that fails or whose log shows a funding clash is stopped and launched
    again on the same account, CLUSTER_TRIES times in all, before the stack gives up."""
    def one(j, i):
        rname, m, k = plan[i]
        for attempt in range(1, CLUSTER_TRIES + 1):
            t0 = time.time()
            launch_cluster(roles, rname, m, k, extras[i], token, funder_key=funders[j + 1])
            try:
                wait_cluster(roles, rname, m, "spawned")
                err = None
            except SystemExit as e:
                err = str(e)
            clash = funding_clashes(roles, rname, m)
            if not err and not clash:
                return round(time.time() - t0), attempt
            say(f"{rname} on {m['name']}: attempt {attempt} failed after {round(time.time() - t0)}s: "
                + "; ".join(x for x in (err, clash) if x)[:500])
            stop_cluster(roles, rname, m)
        raise SystemExit(f"{rname} on {m['name']}: localcluster failed {CLUSTER_TRIES} times")

    with funding_lock():
        t0 = time.time()
        with ThreadPoolExecutor(max(1, parallel)) as ex:
            res = list(ex.map(lambda ji: one(*ji), enumerate(order)))
    again = sum(1 for _, a in res if a > 1)
    say(f"{len(order)} localcluster(s) funded from their own accounts, {parallel} at a time, spawned within "
        f"{round(time.time() - t0)}s (slowest {max(t for t, _ in res)}s); {again} needed another attempt")


def start_clusters(roles, n_relays, n_exits, n_extras, token, adopt=()):
    """Every relay and exit machine's localcluster, one after the other (see the module docstring), then all ready.
    Returns [(role, machine, status)] in global id order: relays first. Roles in `adopt` are not started: their
    clusters are already running (`relay-start`), only their status is read; they mint no client identities."""
    plan = [("relays", m, k) for m, k in spread(n_relays, roles["relays"]["machines"])] + \
           [("exits", m, k) for m, k in spread(n_exits, roles["exits"]["machines"])]
    # the clients' identities: a localcluster mints at most 5; spread evenly over the clusters (each identity costs its
    # cluster three funding transactions, so the first clusters minting 5 each held up everything behind them)
    extras, left = [0] * len(plan), n_extras
    own = [i for i, (rname, _, _) in enumerate(plan) if rname not in adopt]
    while left and any(extras[i] < 5 for i in own):
        for i in own:
            if left and extras[i] < 5:
                extras[i] += 1
                left -= 1
    if left:
        raise SystemExit(f"{n_extras} client identities need at least {-(-n_extras // 5)} node machines (5 per localcluster)")
    if len(plan) == 2 and plan[0][2] == plan[1][2] == 1 and not adopt:
        raise SystemExit("one relay and one exit: neither cluster can become ready alone; use at least three nodes")
    # larger clusters first; a one-node cluster started first becomes ready once the next one's nodes come up
    order = [i for i in sorted(range(len(plan)), key=lambda i: -plan[i][2]) if plan[i][0] not in adopt]
    stagger = float(os.environ.get("MULTIHOST_LAUNCH_STAGGER_S", LAUNCH_STAGGER_S))
    funders = load_funders()
    if funders and len(funders) >= len(order) and supports_funder_key(roles, {plan[i][0] for i in order}):
        funded_launch(roles, plan, order, extras, token, funders, int(os.environ.get("MULTIHOST_LAUNCH_PARALLEL", LAUNCH_PARALLEL)))
    elif stagger <= 0:
        for i in order:
            rname, m, k = plan[i]
            with funding_lock():
                launch_cluster(roles, rname, m, k, extras[i], token)
                wait_cluster(roles, rname, m, "spawned")
    else:
        staggered_launch(roles, plan, order, extras, token, stagger)
    out = []
    for rname, m, k in plan:
        st = wait_cluster(roles, rname, m, "running")
        if rname in adopt:
            st = dict(st, extras=[])        # identities an adopted cluster minted at its first start are not reused
        out.append((rname, m, st))
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
    # a container of that name from an earlier stack would be "already running" and skipped (client.sh start)
    h.run(f"docker rm -f {name} {name}-tools >/dev/null 2>&1; true", timeout=120)
    r = h.run(f"cd {repo} && CONFIG_DIR={REMOTE_CONFIG_DIR} CLIENT_NO_RM=1 {keep} just client-start-one {name} {sd} {extra} {config_file}",
              timeout=600)
    if r.returncode:
        raise SystemExit(f"{name} on {m['name']}: {(r.stdout + r.stderr).strip()[-400:]}")
    say(f"{name} started on {m['name']}")
    return m


def client_state(roles, k, name):
    """(container running?, first line of its status) of client k on its machine."""
    h = client_machine(roles, k)["host"]
    running = h.out(f"docker inspect -f '{{{{.State.Running}}}}' {name} 2>/dev/null", timeout=60) == "true"
    head = h.out(f"docker exec {name} gnosis_vpn-ctl status 2>/dev/null | head -1", timeout=60) if running else ""
    return running, head


def restart_client(roles, k, name):
    """The client container and then its sidecar (whose network namespace is the client's)."""
    client_machine(roles, k)["host"].run(f"docker restart {name} >/dev/null && sleep 3 && docker restart {name}-tools >/dev/null", timeout=180)


def settle_restart(roles, clients, batch=8):
    """Every client restarted once the whole topology's channels are open, then waited for until Ready. A client started
    before the network was complete kept failing its exit's health check ("Version check error: Request timed out")
    until restarted: 5, 10 and 13 of 30 clients in three bring-ups on 2026-10-08/09, a restart cured most of them."""
    t0 = time.time()

    def one(c):
        restart_client(roles, c["k"], c["name"])
        t1 = time.time()
        while time.time() - t1 < CLIENT_READY_S:
            running, head = client_state(roles, c["k"], c["name"])
            if running and head.startswith("Ready"):
                return None
            time.sleep(5)
        return c["name"]

    with ThreadPoolExecutor(batch) as ex:
        late = [x for x in ex.map(one, clients) if x]
    say(f"settle: {len(clients)} client(s) restarted with the network complete, Ready again in {round(time.time() - t0)}s"
        + (f"; not Ready: {', '.join(late)}" if late else ""))


def fresh_start_client(roles, cfg, env, c):
    """Client c from nothing: its container, sidecar and state dir removed, then started like the first time."""
    state = env.get("CLIENT_STATE_DIR") or "/tmp/gnosis_vpn-testenv-state"
    sd = state if c["k"] == 1 else f"{state}-{c['k']}"
    client_machine(roles, c["k"])["host"].run(f"docker rm -f {c['name']} {c['name']}-tools >/dev/null 2>&1; rm -rf {shlex.quote(sd)}; true",
                                             timeout=120)
    start_clients(roles, cfg, env, [(c["name"], c["k"], c["extra"], c["config"])], batch=1)


def save_client_log(roles, cfg, k, name, why):
    h = client_machine(roles, k)["host"]
    d = cfg.config_dir / "client-logs"
    d.mkdir(exist_ok=True)
    f = d / f"{name}-{time.strftime('%H%M%S', time.gmtime())}.log"
    f.write_text(f"== {why}\n" + h.out(f"docker logs {name} 2>&1 | grep -v ' TRACE ' | tail -400", timeout=120))
    return f


def start_clients(roles, cfg, env, specs, batch=None):
    """specs [(name, k, extra, config)], started `batch` at a time; each must be running and Ready (its node up, its
    Safe registered) within CLIENT_READY_S, else its log is saved under CONFIG_DIR/client-logs and it is started again,
    CLIENT_TRIES times in all."""
    batch = batch or int(os.environ.get("MULTIHOST_CLIENT_BATCH", CLIENT_BATCH))

    def one(spec):
        name, k, extra, config = spec
        for attempt in range(1, CLIENT_TRIES + 1):
            start_client(roles, cfg, env, name, k, extra, config)
            t0 = time.time()
            while time.time() - t0 < CLIENT_READY_S:
                running, head = client_state(roles, k, name)
                if running and head.startswith("Ready"):
                    return attempt, round(time.time() - t0)
                if not running:
                    break
                time.sleep(5)
            f = save_client_log(roles, cfg, k, name, f"attempt {attempt}: running={running}, status {head!r}")
            say(f"{name}: attempt {attempt} {'exited' if not running else f'not Ready after {CLIENT_READY_S}s'} ({head[:80]}); log {f.name}")
        raise SystemExit(f"{name} did not come up in {CLIENT_TRIES} attempts (logs in {cfg.config_dir / 'client-logs'})")

    t0, again = time.time(), 0
    for i in range(0, len(specs), batch):
        with ThreadPoolExecutor(batch) as ex:
            res = list(ex.map(one, specs[i:i + batch]))
        again += sum(1 for a, _ in res if a > 1)
    say(f"{len(specs)} client(s) Ready, {batch} at a time, in {round(time.time() - t0)}s; {again} needed another attempt")


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
    """Everything off. --keep-chain: the chain and the relay clusters stay (a relay sweep swaps relays under a running
    chain); clients, VPN servers, target and exit clusters go."""
    if roles is None:
        opts, roles = load_hosts(args.hosts)
    keep = getattr(args, "keep_chain", False)
    env = dict(os.environ)
    stop_clients(roles, env)
    ex = roles["exits"]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda m: m["host"].run(f"cd {ex['repo']} && SERVER_COUNT=8 just server-stop target-stop >/dev/null 2>&1; true",
                                              timeout=300), ex["machines"]))
        list(pool.map(lambda rm: stop_cluster(roles, rm[0], rm[1]),
                      [(r, m) for r in (("exits",) if keep else ("exits", "relays")) for m in roles[r]["machines"]]))
    if not keep:
        chain(roles)["host"].run(f"docker rm -f {CHAIN_NAME} >/dev/null 2>&1; true", timeout=120)
    cfg = Config()
    for f in ("multihost.json", "multihost.env", relaytopo.TOPOLOGY_FILE):
        (cfg.config_dir / f).unlink(missing_ok=True)
    say("multihost stack down")


def up(args):
    opts, roles = load_hosts(args.hosts)
    lay = layout(args.mode, args.n, args.server_per_client)
    n_relays, n_exits, n = len(lay["relays"]), len(lay["exits"]), lay["n"]
    down(args, opts, roles)
    write_ssh_config(opts, roles)
    cfg = Config()
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    if args.keep_chain:
        if Host(None).out(f"curl -s -o /dev/null -m 5 -w '%{{http_code}}' {chain_url(roles)}/", timeout=20) in ("", "000"):
            raise SystemExit(f"--keep-chain: no chain at {chain_url(roles)}")
        merged = merge(roles, opts, start_clusters(roles, n_relays, n_exits, n, secrets.token_hex(16), adopt=("relays",)))
    else:
        start_chain(roles)
        prepare_funders(roles)
        merged = merge(roles, opts, start_clusters(roles, n_relays, n_exits, n, secrets.token_hex(16)))
    status_file = cfg.config_dir / "multihost.json"
    (cfg.config_dir / "blokli_url").write_text(merged["blokli_url"] + "\n")
    fetch_extras(opts, merged, cfg)
    machine_of = {nd["id"]: nd["machine"] for nd in merged["nodes"]}
    # the VPN servers: in the relay topologies one per exit node on that exit's machine, numbered per machine (so one
    # per client in paired and shared, one for everybody in single-exit); in standard one server every client shares
    topo = None
    if args.mode == "standard":
        servers = {machine_of[lay["exits"][0]]: 1}
    else:
        topo = relaytopo.with_addresses(lay, merged)
        servers, slot = {}, {}
        for c in topo["clients"]:
            mname = machine_of[c["exit"]]
            key = (c["exit"], c["server"])                     # the layout's server: shared per exit, or one per client
            if key not in slot:
                slot[key] = servers.get(mname, 0)
                servers[mname] = slot[key] + 1
            c["server"] = slot[key]
        topo["servers"] = sum(servers.values())
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
        specs = [(c["name"], c["k"], c["extra"], c["config"]) for c in topo["clients"]]
        if os.environ.get("MULTIHOST_BATCH_CHANNELS", "0") != "0":
            # MULTIHOST_BATCH_CHANNELS=1: a batch's channels open before the next batch starts. Off by default: it was a
            # guess at clients missing events while others opened channels, and did not help (12 of 16 still failed);
            # the cause was the client's sync_tolerance (relaytopo.render_client_config)
            batch = int(os.environ.get("MULTIHOST_CLIENT_BATCH", CLIENT_BATCH))
            for i in range(0, len(specs), batch):
                part = specs[i:i + batch]
                start_clients(roles, cfg, env, part, batch=batch)
                ks, t1 = {k for _, k, _, _ in part}, time.time()
                while True:
                    problems, _ = relaytopo.check_channels(topo, full_topology(cluster, topo["relays"][0]), clients=ks)
                    if not problems or time.time() - t1 > 300:
                        break
                    time.sleep(10)
                say(f"batch {i // batch + 1}: {len(part) - len(problems)} of {len(part)} channel(s) open in {int(time.time() - t1)}s"
                    + (f"; left to the wait below: {', '.join(p.split(':')[0] for p in problems)}" if problems else ""))
        else:
            start_clients(roles, cfg, env, specs)
        say("waiting for every client's channel to its relay")
        t0, said, revived = time.time(), 0, {}         # revived: client name -> (times, last at)
        by_name = {c["name"]: c for c in topo["clients"]}
        while True:
            problems, summary = relaytopo.check_channels(topo, full_topology(cluster, topo["relays"][0]))
            if not problems:
                break
            if time.time() - t0 > args.timeout:
                for p in problems:
                    say("  ", p)
                # up to MULTIHOST_ALLOW_MISSING clients without their channel are let through: `doctor` finds them failing
                # and moves them last, and no rung reaches them (better than a fresh chain for one client, 2026-10-09)
                allow = int(os.environ.get("MULTIHOST_ALLOW_MISSING", "0"))
                lacking = [p.split(":")[0] for p in problems]
                if len(problems) <= allow and all(p.split(":")[0] in by_name and "0 channels out" in p for p in problems):
                    topo["missing_channels"] = lacking
                    say(f"topology reached without the channels of {', '.join(lacking)} after {args.timeout}s "
                        f"(MULTIHOST_ALLOW_MISSING={allow}); doctor will move them last")
                    break
                raise SystemExit(f"topology not reached after {args.timeout}s")
            if time.time() - said >= 60:
                said = time.time()
                waiting = [p.split(":")[0] for p in problems]
                say(f"  {len(waiting)} channel(s) still missing after {int(time.time() - t0)}s: {', '.join(waiting[:12])}"
                    + (" ..." if len(waiting) > 12 else ""))
                # a client without its channel 3 min after the wait began, or 3 min after the last try, is tried again, 3
                # times at most: a restart first (2026-10-08: it opened the channel of 4 of 6 such clients within a
                # minute), then a fresh start (container and state dir gone, the identity copied in again). A client
                # that died is started again at once. Not on "could not find packet keys for channel endpoints": every
                # client logs that at start, for every channel, and again after a restart
                for nm in waiting if os.environ.get("MULTIHOST_REVIVE", "1") != "0" else []:
                    c = by_name.get(nm)
                    if not c:
                        continue
                    times, last = revived.get(nm, (0, t0))
                    alive = client_state(roles, c["k"], nm)[0]
                    if times >= 3 or (alive and time.time() - last < 180):
                        continue
                    revived[nm] = (times + 1, time.time())
                    save_client_log(roles, cfg, c["k"], nm, f"no channel after {int(time.time() - t0)}s, try {times + 1}")
                    if alive and times == 0:
                        say(f"  {nm}: running, no channel after {int(time.time() - t0)}s; restarting it")
                        restart_client(roles, c["k"], nm)
                    else:
                        say(f"  {nm}: {'not running' if not alive else 'still no channel'} after {int(time.time() - t0)}s; "
                            f"starting it afresh (try {times + 1})")
                        fresh_start_client(roles, cfg, env, c)
            time.sleep(15)
        say(f"topology reached after {int(time.time() - t0)}s: " + "; ".join(f"{k}: {v}" for k, v in summary.items()))
        if os.environ.get("MULTIHOST_SETTLE_RESTART", "0") != "0":
            settle_restart(roles, topo["clients"])
        topo["ready"] = time.strftime("%FT%TZ", time.gmtime())
        relaytopo.save(cfg.config_dir, topo)
    (cfg.config_dir / "multihost.env").write_text(
        f"export MULTIHOST_STATUS={shlex.quote(str(status_file))} MULTIHOST_SSH_OPTS={shlex.quote(opts)} "
        f"TARGET_HOST={target_ip} DEST={dest} CLUSTER_SIZE={n_relays + n_exits} CLIENT_COUNT={n}\n")
    say(f"multihost {args.mode} n={n} up: {n_relays} relay(s) on {len(roles['relays']['machines'])} machine(s), {n_exits} exit(s) "
        f"on {len(roles['exits']['machines'])}, {n} client(s) on {len(roles['clients']['machines'])}; target {target_ip}; "
        f"env in {cfg.config_dir / 'multihost.env'}")


def relay_start(args):
    """A one-node relay cluster on every relay machine of HOSTS that runs none, on the running chain (`up` with
    --keep-chain adopts them later): launched one at a time under the funding lock, then each waited for until
    ready (/readyz needs a peer: some other node must be up on the chain)."""
    opts, roles = load_hosts(args.hosts)
    r = roles["relays"]
    if Host(None).out(f"curl -s -o /dev/null -m 5 -w '%{{http_code}}' {chain_url(roles)}/", timeout=20) in ("", "000"):
        raise SystemExit(f"no chain at {chain_url(roles)}")
    todo = []
    for m in r["machines"]:
        try:
            st = json.loads(m["host"].out(f"{r['localcluster_bin']} status --data-dir {DATA['relays']}", timeout=60) or "{}")
        except ValueError:
            st = {}
        if st.get("state") == "running":
            say(f"relay on {m['name']}: already running")
            continue
        funders = load_funders()
        key = funders.get(FUNDER_ACCOUNTS - 1 - r["machines"].index(m)) if funders and supports_funder_key(roles, ["relays"]) else None
        with funding_lock():
            launch_cluster(roles, "relays", m, 1, 0, secrets.token_hex(16), funder_key=key)
            wait_cluster(roles, "relays", m, "spawned")
        todo.append(m)
    for m in todo:
        st = wait_cluster(roles, "relays", m, "running")
        say(f"relay on {m['name']} ready: {[nd.get('address') for nd in st.get('nodes', [])]}")


def check_client(roles, c, target, ready_s):
    """None when client c connects to its destination and pings the target through the tunnel, else why not."""
    h, n, d = client_machine(roles, c["k"])["host"], c["name"], c["dest"]

    def status():
        return h.out(f"docker exec {n} gnosis_vpn-ctl status 2>/dev/null", timeout=60)

    def dest_line():
        return next((l for l in status().splitlines() if l.startswith(f"{d} Route health:")), "")

    if h.out(f"docker inspect -f '{{{{.State.Running}}}}' {n} 2>/dev/null", timeout=60) != "true":
        return "container not running"
    t0 = time.time()
    while "Ready to connect" not in dest_line():
        if time.time() - t0 > ready_s:
            return f"{d} not Ready to connect after {ready_s}s: {dest_line()[:160]}"
        time.sleep(5)
    h.run(f"docker exec {n} gnosis_vpn-ctl connect {d} >/dev/null 2>&1", timeout=60)
    try:
        t0 = time.time()
        while not any(l.startswith("Connected to") for l in status().splitlines()):
            if time.time() - t0 > 120:
                path = h.out(f"docker logs --since 3m {n} 2>&1 | grep -c 'cannot find 1 hop path'", timeout=60)
                return f"connect to {d} not up after 120s ({path or 0} 'cannot find 1 hop path' in 3 min): {dest_line()[:120]}"
            time.sleep(2)
        got = h.out(f"docker exec {n}-tools ping -c 5 -i 0.5 -W 3 {target} 2>&1 | grep -oE '[0-9]+ received'", timeout=60)
        if not got or got.startswith("0 "):
            return f"connected to {d}, but no ping answer from {target} through the tunnel ({got or 'no ping output'})"
        return None
    finally:
        h.run(f"docker exec {n} gnosis_vpn-ctl disconnect >/dev/null 2>&1", timeout=60)
        for _ in range(30):
            if not any(l.startswith("Connected to") for l in status().splitlines()):
                break
            time.sleep(1)


def doctor(args):
    """Every client of the running stack (`--parallel` at a time): container running, destination Ready to connect,
    connect, ping the target through the tunnel, disconnect. With --repair a client that fails is restarted with its
    sidecar (the cure for "cannot find 1 hop path", 2026-10-08) and checked once more. Exit 1 when any still fails:
    a rung with that client would fail on its connect alone, the rig and not the relay."""
    opts, roles = load_hosts(args.hosts)
    cfg = Config()
    topo = relaytopo.load(cfg.config_dir)
    if not topo:
        raise SystemExit(f"no relay topology in {cfg.config_dir}; doctor checks a paired/shared/single-exit stack")
    target = re.search(r"TARGET_HOST=(\S+)", (cfg.config_dir / "multihost.env").read_text()).group(1)

    env = dict(os.environ)

    def one(c):
        why = check_client(roles, c, target, args.ready)
        if why and args.repair:
            n = c["name"]
            save_client_log(roles, cfg, c["k"], n, f"doctor: {why}")
            restart_client(roles, c["k"], n)
            again = check_client(roles, c, target, max(args.ready, 300))
            if again:
                # a second repair: the client from nothing (container and state dir gone, identity copied in again)
                save_client_log(roles, cfg, c["k"], n, f"doctor after a restart: {again}")
                fresh_start_client(roles, cfg, env, c)
                again = check_client(roles, c, target, max(args.ready, 300))
            return c["name"], why, again
        return c["name"], why, why

    t0 = time.time()
    with ThreadPoolExecutor(args.parallel) as ex:
        res = list(ex.map(one, topo["clients"]))
    for name, first, last in res:
        if first:
            say(f"  {name}: {first}" + (("; after repair: " + (last or "ok")) if args.repair else ""))
    bad = [name for name, _, last in res if last]
    fixed = [name for name, first, last in res if first and not last]
    say(f"doctor: {len(res) - len(bad)} of {len(res)} clients connect and ping through their tunnel in {round(time.time() - t0)}s"
        + (f"; repaired: {', '.join(fixed)}" if fixed else "") + (f"; STILL FAILING: {', '.join(bad)}" if bad else ""))
    # the clients that still fail go to the end of the topology: a rung of n clients takes the first n, so every rung up to
    # the number of healthy clients runs on clients that connect (2026-10-08: ~2 of 30 per bring-up stayed unable to
    # reach their exit after a restart, with nothing in their log)
    healthy = [c for c in topo["clients"] if c["name"] not in bad]
    if bad:
        topo["clients"] = healthy + [c for c in topo["clients"] if c["name"] in bad]
        topo["doctor_failing"] = bad
    else:
        topo.pop("doctor_failing", None)
    relaytopo.save(cfg.config_dir, topo)
    (cfg.config_dir / "doctor.json").write_text(json.dumps({"at": time.strftime("%FT%TZ", time.gmtime()), "healthy": len(healthy),
                                                            "failing": bad, "repaired": fixed}) + "\n")
    if bad and len(bad) <= args.allow_failing:
        say(f"doctor: {len(bad)} failing client(s) moved to the end of the topology; rungs up to {len(healthy)} clients use healthy ones "
            f"(--allow-failing {args.allow_failing})")
    raise SystemExit(1 if len(bad) > args.allow_failing else 0)


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
    u.add_argument("--server-per-client", action="store_true", help="single-exit: a VPN server per client, as the 2026-09-30 run had it")
    u.add_argument("--keep-chain", action="store_true", help="use the running chain and adopt the relays' running clusters (relay-start)")
    d = sub.add_parser("down", help="stop everything on every machine")
    d.add_argument("--keep-chain", action="store_true", help="leave the chain and the relay clusters running")
    sub.add_parser("relay-start", help="a one-node relay cluster on every relay machine that runs none (the chain must be up)")
    sub.add_parser("check", help="what each machine has")
    dr = sub.add_parser("doctor", help="every client of the running stack connects and pings through its tunnel")
    dr.add_argument("--repair", action="store_true", help="restart a failing client and its sidecar, then check it again")
    dr.add_argument("--parallel", type=int, default=8)
    dr.add_argument("--ready", type=int, default=180, help="seconds a destination may take to be Ready to connect")
    dr.add_argument("--allow-failing", type=int, default=0, help="pass with up to this many clients still failing (moved to the end)")
    p = sub.add_parser("provision", help="copy what the machines are missing from this one")
    p.add_argument("--parallel", type=int, default=8)
    args = ap.parse_args()
    {"up": up, "down": down, "check": check, "provision": provision, "relay-start": relay_start, "doctor": doctor}[args.cmd](args)


if __name__ == "__main__":
    main()
