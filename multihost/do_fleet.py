#!/usr/bin/env python3
"""A DigitalOcean fleet for the multi-machine testenv: one droplet per client, per relay and per exit, plus a control
droplet (the chain and the suite). Runs on the operator's machine; the API token never leaves it.

    python3 multihost/do_fleet.py create  --name rs1 --clients 5 --relays 5 --exits 5
    python3 multihost/do_fleet.py wait    --name rs1          # active + first-boot setup done on every droplet
    python3 multihost/do_fleet.py hosts   --name rs1 > hosts.toml
    python3 multihost/do_fleet.py destroy --name rs1          # deletes every droplet of the fleet, then checks

The token is read from DO_API_KEY_FILE (default ~/DO_API_KEY) and sent only to api.digitalocean.com. Nothing here
prints it, writes it to a file or passes it to a droplet. It needs droplet read/create/delete; the fleet is found again
by its name prefix (`gvpn-<name>-`) and the ids saved in ~/.gvpn-fleets/<name>.json, so no tag or account-key scope is
needed: the SSH key goes in through the first-boot script.

Every droplet boots Ubuntu 24.04 (the release binaries need glibc 2.39) and installs docker, just, WireGuard tools and
pytest from its first-boot script, then writes /var/lib/gvpn-ready. The defaults are a dedicated General Purpose
4-vCPU droplet (`g-4vcpu-16gb`, regular Intel) in lon1: on 2026-09-30 fra1 offered no dedicated-CPU size at all."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.digitalocean.com/v2"
STATE = Path.home() / ".gvpn-fleets"
READY = "/var/lib/gvpn-ready"
BAKED = "/var/lib/gvpn-baked"          # present on a droplet made from a `bake` snapshot: tools, images, binaries, repo
USER_DATA = """#!/bin/bash
set -x
# without an account SSH key DigitalOcean gives root an expiring password, and sshd then demands a password change
# before any command, key or not: unexpire it first
chage -d "$(date +%F)" -M 99999 root
install -d -m 700 /root/.ssh
echo '{pubkey}' >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
# a droplet from a baked snapshot has everything already: only docker has to be up
if [ -f """ + BAKED + """ ]; then
  systemctl start docker; for i in $(seq 1 30); do docker version >/dev/null 2>&1 && break; sleep 1; done
  docker version >/dev/null && touch """ + READY + """
  exit 0
fi
export DEBIAN_FRONTEND=noninteractive
for i in $(seq 1 60); do apt-get update -qq && break; sleep 5; done
apt-get install -y -qq docker.io wireguard-tools gettext-base python3-pytest jq curl git iproute2 >/var/log/gvpn-apt.log 2>&1
systemctl enable --now docker
curl -fsSL --retry 5 -o /tmp/just.tgz https://github.com/casey/just/releases/download/1.58.0/just-1.58.0-x86_64-unknown-linux-musl.tar.gz
tar -xzf /tmp/just.tgz -C /tmp just && install -m 0755 /tmp/just /usr/local/bin/just
modprobe wireguard || true
docker version >/dev/null && command -v just && command -v wg && touch """ + READY + "\n"


def token():
    path = Path(os.environ.get("DO_API_KEY_FILE", Path.home() / "DO_API_KEY")).expanduser()
    t = path.read_text().strip()
    if not t:
        raise SystemExit(f"{path} is empty")
    return t


def api(method, path, body=None, timeout=60):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"})
    last = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code == 429 or e.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            raise SystemExit(f"DigitalOcean {method} {path}: HTTP {e.code} {msg}")
        except urllib.error.URLError as e:
            time.sleep(5 * (attempt + 1))
            last = e
    raise SystemExit(f"DigitalOcean {method} {path}: gave up after 5 attempts ({last})")


def prefix(name):
    return f"gvpn-{name}-"


def fleet_droplets(name):
    out, page = [], 1
    while True:
        d = api("GET", f"/droplets?per_page=200&page={page}")
        out += [x for x in d["droplets"] if x["name"].startswith(prefix(name))]
        if not d.get("links", {}).get("pages", {}).get("next"):
            return out
        page += 1


def addrs(d):
    v4 = d["networks"]["v4"]
    pub = next((n["ip_address"] for n in v4 if n["type"] == "public"), "")
    priv = next((n["ip_address"] for n in v4 if n["type"] == "private"), "")
    return pub, priv


def state_file(name):
    STATE.mkdir(exist_ok=True)
    return STATE / f"{name}.json"


def create(a):
    if fleet_droplets(a.name):
        raise SystemExit(f"a fleet '{a.name}' already exists; destroy it or pick another --name")
    pub = Path(a.pubkey).expanduser().read_text().strip()
    groups = [(["control"], a.size)] + [([f"{role}-{i}" for i in range(1, n + 1)], a.size)
                                        for role, n in (("client", a.clients), ("relay", a.relays), ("exit", a.exits)) if n]
    for spec in a.add or []:                                # extra groups with their own size: "relay16:c-16:1"
        group, size, count = spec.split(":")
        groups.append(([f"{group}-{i}" for i in range(1, int(count) + 1)], size))
    names, ids = [], []
    for members, size in groups:
        full = [prefix(a.name) + m for m in members]
        names += full
        for i in range(0, len(full), 10):                   # the API takes at most 10 names per request
            image = int(a.image) if str(a.image).isdigit() else a.image     # a snapshot is an id, a distribution a slug
            body = {"names": full[i:i + 10], "region": a.region, "size": size, "image": image, "ipv6": False,
                    "monitoring": False, "user_data": USER_DATA.format(pubkey=pub)}
            ids += [d["id"] for d in api("POST", "/droplets", body)["droplets"]]
    state_file(a.name).write_text(json.dumps({"name": a.name, "ids": ids, "region": a.region, "size": a.size,
                                              "created": time.strftime("%FT%TZ", time.gmtime())}, indent=2))
    print(f"created {len(ids)} droplets in {a.region}: " + "; ".join(f"{len(m)} x {s}: {', '.join(m)}" for m, s in groups))


def wait(a):
    import subprocess
    key = str(Path(a.key).expanduser())
    want = set(json.loads(state_file(a.name).read_text())["ids"]) if state_file(a.name).exists() else set()
    t0 = time.time()
    while True:
        ds = fleet_droplets(a.name)
        # the listing lags creation: wait for every id created, not for whatever the list shows yet
        pending = [d["name"] for d in ds if d["status"] != "active" or not addrs(d)[0]] + \
                  [f"id {i} (not listed yet)" for i in want - {d["id"] for d in ds}]
        if not pending and ds:
            break
        if time.time() - t0 > a.timeout:
            raise SystemExit(f"not active after {a.timeout}s: {pending}")
        time.sleep(10)
    print(f"all {len(ds)} active after {int(time.time() - t0)}s; waiting for first-boot setup")
    todo = {d["name"]: addrs(d)[0] for d in ds}
    while todo:
        for n, ip in list(todo.items()):
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
                                "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-i", key, "-o", "IdentitiesOnly=yes",
                                f"root@{ip}", f"test -f {READY}"], capture_output=True, timeout=30)
            if r.returncode == 0:
                del todo[n]
        if todo and time.time() - t0 > a.timeout:
            raise SystemExit(f"first-boot setup not done after {a.timeout}s: {sorted(todo)}")
        if todo:
            time.sleep(15)
    print(f"fleet '{a.name}' ready after {int(time.time() - t0)}s")


def ssh_argv(key, target, known="/dev/null"):
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known}", "-o", "LogLevel=ERROR", "-i", str(Path(key).expanduser()), "-o", "IdentitiesOnly=yes", target]


def sh(cmd, timeout=1800):
    """A local shell pipeline (ssh on both ends of a docker save | docker load); raises on failure."""
    import subprocess
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise SystemExit(f"failed ({r.returncode}): {cmd[:160]}...\n{(r.stdout + r.stderr).strip()[-600:]}")
    return r.stdout


def action(droplet_id, body, timeout=1800):
    """Run a droplet action (power_off, snapshot) and wait for it to complete."""
    act = api("POST", f"/droplets/{droplet_id}/actions", body)["action"]
    t0 = time.time()
    while act["status"] == "in-progress":
        if time.time() - t0 > timeout:
            raise SystemExit(f"{body['type']} on {droplet_id} still in progress after {timeout}s")
        time.sleep(10)
        act = api("GET", f"/actions/{act['id']}")["action"]
    if act["status"] != "completed":
        raise SystemExit(f"{body['type']} on {droplet_id}: {act['status']}")
    return act


def bake(a):
    """A snapshot with everything a fleet droplet needs, so droplets made from it skip their installs and provisioning:
    the first-boot tools, the images (client, tools sidecar, server, target, chain), the node binaries and the repo.
    Built on a throwaway droplet (gvpn-bake-<name>), which is deleted afterwards whatever happens."""
    import subprocess
    shlex = __import__("shlex")
    name = f"bake-{a.snapshot}"
    b = argparse.Namespace(name=name, clients=0, relays=0, exits=0, region=a.region, size=a.size, image="ubuntu-24-04-x64",
                           pubkey=a.key + ".pub", add=None)
    create(b)
    try:
        wait(argparse.Namespace(name=name, key=a.key, timeout=1200))
        d = fleet_droplets(name)[0]
        ip = addrs(d)[0]
        dst = " ".join(shlex.quote(x) for x in ssh_argv(a.key, f"root@{ip}"))
        src = " ".join(shlex.quote(x) for x in ssh_argv(a.source_key, a.source, known=str(Path.home() / ".ssh" / "known_hosts")))
        print(f"builder {d['name']} {ip}: loading images from {a.source}")
        sh(f"{src} {shlex.quote('docker save ' + ' '.join(a.images) + ' | gzip -1')} | {dst} 'gunzip | docker load'")
        print("node binaries")
        sh(f"{dst} 'mkdir -p /root/relayscale/bin && cd /root/relayscale/bin && curl -fsSL --retry 3 -o hoprd-4.1.3-release "
           f"{a.hoprd_url} && chmod +x hoprd-4.1.3-release'")
        got = sh(f"{dst} 'sha256sum /root/relayscale/bin/hoprd-4.1.3-release'").split()[0]
        if got != a.hoprd_sha256:
            raise SystemExit(f"hoprd checksum mismatch: {got} != {a.hoprd_sha256}")
        sh(f"{src} {shlex.quote('cat ' + a.localcluster)} | {dst} 'cat > /root/relayscale/bin/hoprd-localcluster && chmod +x /root/relayscale/bin/hoprd-localcluster'")
        want = sh(f"{src} {shlex.quote('sha256sum ' + a.localcluster)}").split()[0]
        got = sh(f"{dst} 'sha256sum /root/relayscale/bin/hoprd-localcluster'").split()[0]
        if want != got:
            raise SystemExit(f"localcluster checksum mismatch: {want} != {got}")
        print("chain image and repo")
        sh(f"{dst} 'docker pull -q {a.chain_image}@{a.chain_digest} >/dev/null && docker tag {a.chain_image}@{a.chain_digest} {a.chain_image}:latest "
           f"&& git clone -q -b {a.branch} {a.repo_url} /root/relayscale/gnosis_vpn-testenv'")
        manifest = (f"gvpn snapshot {a.snapshot} built {time.strftime('%FT%TZ', time.gmtime())}; images: {' '.join(a.images)} {a.chain_image}@{a.chain_digest}; "
                    f"hoprd sha256 {a.hoprd_sha256}; localcluster sha256 {want}; repo {a.repo_url} {a.branch}")
        # clear the first boot's traces: new droplets run cloud-init as new instances, add their own key, regenerate
        # host keys; the baked marker sends them down the short path of the first-boot script
        sh(f"{dst} {shlex.quote(f'echo {shlex.quote(manifest)} > {BAKED} && rm -f {READY} && : > /root/.ssh/authorized_keys && apt-get clean && journalctl --vacuum-size=1M >/dev/null 2>&1; cloud-init clean --logs --seed; sync')}")
        print("powering off and snapshotting (several minutes)")
        action(d["id"], {"type": "power_off"})
        action(d["id"], {"type": "snapshot", "name": a.snapshot}, timeout=3600)
        snaps = [x for x in api("GET", "/snapshots?resource_type=droplet&per_page=200")["snapshots"] if x["name"] == a.snapshot]
        if not snaps:
            raise SystemExit(f"snapshot {a.snapshot} not listed after the action completed")
        s = snaps[-1]
        print(f"snapshot {s['name']} id {s['id']} in {s['regions']}, {s['size_gigabytes']} GB (min disk {s['min_disk_size']} GB)")
        print(f"use it: do_fleet.py create --image {s['id']} ...")
    finally:
        destroy(argparse.Namespace(name=name))


def snapshots(a):
    for s in api("GET", "/snapshots?resource_type=droplet&per_page=200")["snapshots"]:
        if s["name"].startswith("gvpn-"):
            print(s["id"], s["name"], s["regions"], f"{s['size_gigabytes']} GB", s["created_at"])


def hosts(a):
    """The multihost hosts file for this fleet, as the control droplet (the runner) sees it: private addresses."""
    ds = {d["name"][len(prefix(a.name)):]: d for d in fleet_droplets(a.name)}
    ctl_pub, ctl = addrs(ds["control"])

    def machines(role):
        group = {"relay": a.relays, "exit": a.exits, "client": a.clients}[role]
        ms = sorted((k for k in ds if k.startswith(group + "-") and k[len(group) + 1:].isdigit()), key=lambda k: int(k.rsplit("-", 1)[1]))
        return "\n".join(f'  {{ ssh = "root@{addrs(ds[k])[1]}", addr = "{addrs(ds[k])[1]}", name = "{k}", public = "{addrs(ds[k])[0]}" }},'
                         for k in ms)

    print(f"""# fleet '{a.name}' in {ds['control']['region']['slug']}: relays = group '{a.relays}', exits = '{a.exits}', clients = '{a.clients}'; control {ctl_pub} / {ctl}
[ssh]
options = "-i /root/.ssh/fleet_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/root/.ssh/fleet_known_hosts"

[roles.chain]
ssh = "local"
addr = "{ctl}"

[roles.relays]
hoprd_bin = "/root/relayscale/bin/hoprd-4.1.3-release"
localcluster_bin = "/root/relayscale/bin/hoprd-localcluster"
machines = [
{machines('relay')}
]

[roles.exits]
hoprd_bin = "/root/relayscale/bin/hoprd-4.1.3-release"
localcluster_bin = "/root/relayscale/bin/hoprd-localcluster"
repo = "/root/relayscale/gnosis_vpn-testenv"
server_image = "gnosis_vpn-server:0.7.0-upstream"
machines = [
{machines('exit')}
]

[roles.clients]
repo = "/root/relayscale/gnosis_vpn-testenv"
machines = [
{machines('client')}
]
""")


def destroy(a):
    ids = set()
    sf = state_file(a.name)
    if sf.exists():
        ids |= set(json.loads(sf.read_text())["ids"])
    ids |= {d["id"] for d in fleet_droplets(a.name)}
    for i in sorted(ids):
        try:
            api("DELETE", f"/droplets/{i}")
        except SystemExit as e:
            if "404" not in str(e):
                print(f"delete {i}: {e}", file=sys.stderr)
    t0 = time.time()
    while True:
        left = [d["name"] for d in fleet_droplets(a.name)]
        if not left:
            print(f"fleet '{a.name}': {len(ids)} droplet(s) deleted, none left")
            sf.unlink(missing_ok=True)
            return
        if time.time() - t0 > 600:
            raise SystemExit(f"still there after 600 s: {left} - delete them in the DigitalOcean console")
        time.sleep(10)


def show(a):
    for d in fleet_droplets(a.name):
        print(d["id"], d["name"], d["status"], d["size_slug"], d["region"]["slug"], *addrs(d))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("bake", help="build a snapshot with everything a fleet droplet needs")
    k.add_argument("--snapshot", required=True, help="snapshot name, e.g. gvpn-4.1.3-0.96.4-2026-09-30")
    k.add_argument("--region", default="lon1")
    k.add_argument("--size", default="g-4vcpu-16gb")
    k.add_argument("--key", default="~/.ssh/do_testenv_fleet_ed25519")
    k.add_argument("--source", required=True, help="ssh target holding the images and the localcluster binary")
    k.add_argument("--source-key", default="~/.ssh/do_probe_ed25519")
    k.add_argument("--images", nargs="+", default=["gnosis_vpn-client:0.96.4-snap-20260929.073145", "gnosis_vpn-suite-tools:latest",
                                                   "gnosis_vpn-server:0.7.0-upstream", "gnosis_vpn-target:latest"])
    k.add_argument("--localcluster", default="/root/testenv/hoprd/target/release/hoprd-localcluster")
    k.add_argument("--hoprd-url", default="https://github.com/hoprnet/hoprd/releases/download/v4.1.3/hoprd-x86_64-linux")
    k.add_argument("--hoprd-sha256", default="28e00b385a4630ccbdab88cbcf59ed266d47141f38f97891732925312e40339d")
    k.add_argument("--chain-image", default="europe-west3-docker.pkg.dev/hoprassociation/docker-images/bloklid-anvil")
    k.add_argument("--chain-digest", default="sha256:c901116c499836aebb22c22dc69e9c235d408a8446e00e0b62841a1db79f70ad")
    k.add_argument("--repo-url", default="https://github.com/SCBuergel/gnosis_vpn-testenv.git")
    k.add_argument("--branch", default="multihost")
    sub.add_parser("snapshots", help="list the gvpn- snapshots")
    for c in ("create", "wait", "hosts", "destroy", "list"):
        p = sub.add_parser(c)
        p.add_argument("--name", required=True, help="fleet name; droplets are gvpn-<name>-<role>-<i>")
        if c == "create":
            p.add_argument("--clients", type=int, default=5)
            p.add_argument("--relays", type=int, default=5)
            p.add_argument("--exits", type=int, default=5)
            p.add_argument("--region", default="lon1")
            p.add_argument("--size", default="g-4vcpu-16gb")
            p.add_argument("--image", default="ubuntu-24-04-x64")
            p.add_argument("--pubkey", default="~/.ssh/do_testenv_fleet_ed25519.pub")
            p.add_argument("--add", action="append", metavar="GROUP:SIZE:COUNT",
                           help="an extra group of droplets with its own size, e.g. relay16:c-16:1 (gvpn-<name>-relay16-1)")
        if c == "hosts":
            p.add_argument("--relays", default="relay", help="the droplet group that plays the relays (relay, relay16, ...)")
            p.add_argument("--exits", default="exit", help="the droplet group that plays the exits")
            p.add_argument("--clients", default="client", help="the droplet group that plays the clients")
        if c == "wait":
            p.add_argument("--key", default="~/.ssh/do_testenv_fleet_ed25519")
            p.add_argument("--timeout", type=int, default=1200)
    a = ap.parse_args()
    {"create": create, "wait": wait, "hosts": hosts, "destroy": destroy, "list": show, "bake": bake, "snapshots": snapshots}[a.cmd](a)


if __name__ == "__main__":
    main()
