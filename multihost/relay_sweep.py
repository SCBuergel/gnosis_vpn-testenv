#!/usr/bin/env python3
"""A relay sweep on DigitalOcean: T34-single-relay-scaling (n clients -> one relay -> an exit per client, n = 1..6,
download only) against one relay machine size after another, on one fleet whose chain, exits and clients stay.

    python3 multihost/relay_sweep.py run --name sw1          # resumable: state in ~/.gvpn-fleets/<name>-sweep.json
    python3 multihost/relay_sweep.py destroy --name sw1      # every droplet gvpn-<name>-*

Runs on the operator's machine (the DigitalOcean token never leaves it, see do_fleet.py). The base fleet: a control
droplet (chain and suite), CLIENTS client machines and as many exit machines, all BASE_SIZE from the baked snapshot.
Relays are made one size at a time from plain Ubuntu and deleted after their test. While relay i is measured, the
next PREPARE relays are created, their specs and a CPU benchmark taken, and their hoprd started on the running chain
(funded, Safe deployed, announced: `multihost.py relay-start`), so the swap only restarts the exits and clients
(`multihost.py up --keep-chain`, fresh identities, every channel to the new relay). No channel to a prepared relay is
opened before its turn: a relay any client or exit holds a channel to carries traffic, and the test pins all of it
to the relay under test.

Per relay: its own run id <name>-<label>; 1-s vmstat on every machine for the length of the test; the specs of every
machine in specs/<droplet>.txt."""
import argparse
import json
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import do_fleet  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
RELAYS = [("c16", "c-16"), ("c8", "c-8"), ("c4", "c-4"), ("c2", "c-2"),
          ("s8", "s-8vcpu-16gb"), ("s4", "s-4vcpu-8gb"), ("s2", "s-2vcpu-4gb"), ("s1", "s-1vcpu-2gb"),
          ("s8b", "s-8vcpu-16gb")]             # s8b: s-8 again on a fresh droplet (sw1-s8 lost rungs 4-6 to one client's connect)
CLIENTS = 6
BASE_SIZE = "c-8"
REGION = "lon1"
SNAPSHOT = 247746824
PREPARE = 3                       # relays made ahead of the one under test
KEY = Path("~/.ssh/do_testenv_fleet_ed25519").expanduser()
SOURCE = "root@139.59.157.121"    # cpu-8: holds the max16 localcluster (random client identities)
SOURCE_KEY = Path("~/.ssh/do_probe_ed25519").expanduser()
LOCALCLUSTER = "/root/testenv/hoprd/target/release/hoprd-localcluster"
LOCALCLUSTER_SHA = "a6c3f881441a4a5eabad7efb60a2c3bc0afb3d194e0975d2bf558510f0ca90a3"
HOPRD_SHA = "28e00b385a4630ccbdab88cbcf59ed266d47141f38f97891732925312e40339d"
RS = "/root/relayscale"
KNOBS = {"T34_LADDER": "1 2 3 4 5 6", "T34_DOWN_BYTES": "100000000", "T34_UP_BYTES": "0", "T34_CAP": "300", "T34_PING": "1"}
SPECS = r"""set +e
echo "### date"; date -u +%FT%TZ
echo "### droplet"; for k in id hostname region; do printf '%s=' $k; curl -s -m 3 http://169.254.169.254/metadata/v1/$k; echo; done
echo "### uname"; uname -a
echo "### os-release"; cat /etc/os-release
echo "### virt"; systemd-detect-virt
echo "### nproc"; nproc
echo "### lscpu"; lscpu
echo "### lscpu -e"; lscpu -e
echo "### cpuinfo"; cat /proc/cpuinfo
echo "### cpu vulnerabilities"; grep . /sys/devices/system/cpu/vulnerabilities/*
echo "### meminfo"; cat /proc/meminfo
echo "### free"; free -m
echo "### dmidecode"; dmidecode -t system -t bios -t processor -t memory 2>/dev/null
echo "### disks"; lsblk -o NAME,SIZE,TYPE,ROTA,MODEL; df -h /
echo "### net"; ip -br addr; ethtool -i eth0 2>/dev/null; ethtool eth0 2>/dev/null | grep -i speed; ethtool -l eth0 2>/dev/null
echo "### sysctl"; sysctl net.core.rmem_max net.core.wmem_max net.core.rmem_default net.core.netdev_max_backlog
echo "### loadavg"; cat /proc/loadavg
echo "### vmstat 1 10 (idle steal)"; vmstat -n -t 1 10
echo "### openssl speed sha256, 1 thread"; openssl speed -seconds 3 sha256 2>/dev/null | tail -2
echo "### openssl speed sha256, $(nproc) threads"; openssl speed -seconds 3 -multi $(nproc) sha256 2>/dev/null | tail -2
echo "### openssl speed aes-128-gcm, 1 thread"; openssl speed -seconds 3 -evp aes-128-gcm 2>/dev/null | tail -2
"""


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def ssh(ip, cmd, timeout=600, check=True, input=None, key=KEY):
    argv = do_fleet.ssh_argv(key, f"root@{ip}") + [cmd]
    for attempt in range(6):        # 255: ssh itself failed (the operator's network), the command did not run
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input)
        except subprocess.TimeoutExpired:
            if attempt == 5:
                raise
            time.sleep(15)
            continue
        if r.returncode != 255:
            break
        time.sleep(15)
    if check and r.returncode:
        raise RuntimeError(f"ssh {ip} failed ({r.returncode}): {cmd[:120]}\n{(r.stdout + r.stderr)[-800:]}")
    return r.stdout


class Sweep:
    def __init__(self, name, out):
        self.name = name
        self.out = Path(out).expanduser()
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "specs").mkdir(exist_ok=True)
        self.state_path = do_fleet.STATE / f"{name}-sweep.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"done": [], "relays": {}}
        self.lock = threading.Lock()

    def save(self):
        with self.lock:
            self.state_path.write_text(json.dumps(self.state, indent=2))

    # -- droplets ------------------------------------------------------------------------------------------------
    def droplets(self):
        return {d["name"][len(do_fleet.prefix(self.name)):]: d for d in do_fleet.fleet_droplets(self.name)}

    def base(self):
        ds = self.droplets()
        if "control" not in ds:
            log(f"creating base fleet {self.name}: control + {CLIENTS} clients + {CLIENTS} exits, {BASE_SIZE}, {REGION}")
            do_fleet.create(argparse.Namespace(name=self.name, clients=CLIENTS, relays=0, exits=CLIENTS, region=REGION, size=BASE_SIZE,
                                               image=str(SNAPSHOT), pubkey=str(KEY) + ".pub", add=None, project=do_fleet.PROJECT))
        do_fleet.wait(argparse.Namespace(name=self.name, key=str(KEY), timeout=1500))
        ds = self.droplets()
        self.ctl = do_fleet.addrs(ds["control"])[0]
        self.ctl_priv = do_fleet.addrs(ds["control"])[1]
        return ds

    def relay_name(self, label):
        return f"relay-{label}"

    def create_relay(self, label, size, start=True):
        """The relay droplet (plain Ubuntu 24.04: c-2's 25 GB disk is below the snapshot's 50 GB minimum, so every relay
        is made the same way), its first boot, specs and benchmark, its binaries, its hoprd on the chain."""
        name = self.relay_name(label)
        full = do_fleet.prefix(self.name) + name
        if self.state["relays"].get(label, {}).get("started"):
            return
        ds = self.droplets()
        if name not in ds:
            log(f"relay {label}: creating {full} ({size})")
            body = {"names": [full], "region": REGION, "size": size, "image": "ubuntu-24-04-x64", "ipv6": False, "monitoring": False,
                    "user_data": do_fleet.USER_DATA.format(pubkey=(Path(str(KEY) + ".pub")).read_text().strip())}
            d = do_fleet.api("POST", "/droplets", body)["droplets"][0]
            do_fleet.assign_to_project(do_fleet.PROJECT, [d["id"]])
        t0 = time.time()
        while True:
            d = self.droplets().get(name)
            if d and d["status"] == "active" and do_fleet.addrs(d)[0]:
                pub = do_fleet.addrs(d)[0]
                if subprocess.run(do_fleet.ssh_argv(KEY, f"root@{pub}") + [f"test -f {do_fleet.READY}"], capture_output=True, timeout=30).returncode == 0:
                    break
            if time.time() - t0 > 1500:
                raise RuntimeError(f"relay {label} not ready after 1500 s")
            time.sleep(15)
        pub, priv = do_fleet.addrs(d)
        with self.lock:
            self.state["relays"][label] = {"size": size, "id": d["id"], "name": full, "public": pub, "private": priv,
                                           "ready": time.strftime("%FT%TZ", time.gmtime())}
        self.save()
        log(f"relay {label}: {full} up after {int(time.time() - t0)} s ({pub} / {priv}); specs and benchmark")
        if not (self.out / "specs" / f"{full}.txt").exists():
            self.specs(full, pub, d)
        o = self.ctl_ssh_opts()
        have = ssh(self.ctl, f"ssh {o} root@{priv} 'sha256sum {RS}/bin/hoprd-4.1.3-release {RS}/bin/hoprd-localcluster 2>/dev/null'", check=False)
        if HOPRD_SHA not in have or LOCALCLUSTER_SHA not in have:
            ssh(self.ctl, f"ssh {o} root@{priv} 'mkdir -p {RS}/bin' && "
                          f"scp -q {o} {RS}/bin/hoprd-4.1.3-release {RS}/bin/hoprd-localcluster root@{priv}:{RS}/bin/", timeout=600)
            have = ssh(self.ctl, f"ssh {o} root@{priv} 'sha256sum {RS}/bin/*'")
            if HOPRD_SHA not in have or LOCALCLUSTER_SHA not in have:
                raise RuntimeError(f"relay {label}: binaries' checksums {have}")
        if not start:
            return
        self.chain_up.wait()
        self.write_hosts(f"prep-{label}", label)
        log(f"relay {label}: starting its hoprd on the chain")
        self.job(f"prep-{label}", f"python3 tests/multihost.py {RS}/hosts-prep-{label}.toml relay-start", timeout=1800)
        with self.lock:
            self.state["relays"][label]["started"] = time.strftime("%FT%TZ", time.gmtime())
        self.save()
        log(f"relay {label}: hoprd running, funded and announced")

    def destroy_relay(self, label):
        r = self.state["relays"].get(label)
        if not r:
            return
        try:
            do_fleet.api("DELETE", f"/droplets/{r['id']}")
        except SystemExit as e:
            if "404" not in str(e):
                raise
        with self.lock:
            r["deleted"] = time.strftime("%FT%TZ", time.gmtime())
        self.save()
        log(f"relay {label}: droplet {r['name']} deleted")

    # -- control -------------------------------------------------------------------------------------------------
    def ctl_ssh_opts(self):
        return "-i /root/.ssh/fleet_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/root/.ssh/fleet_known_hosts -o BatchMode=yes"

    def specs(self, full, pub, d):
        txt = ssh(pub, SPECS, timeout=300, check=False)
        api = {k: d.get(k) for k in ("id", "name", "size_slug", "vcpus", "memory", "disk", "created_at", "image", "kernel", "features")}
        api["region"] = d["region"]["slug"]
        api["size"] = d.get("size")
        (self.out / "specs" / f"{full}.txt").write_text("### digitalocean api\n" + json.dumps(api, indent=2) + "\n" + txt)

    def setup_control(self, ds):
        if self.state.get("control_ready"):
            return
        log("control: fleet key, localcluster (max16), repo tree, env")
        # relays come and go and DigitalOcean hands a deleted relay's private address to the next one: a kept host key
        # then fails every ssh to it ("REMOTE HOST IDENTIFICATION HAS CHANGED"); inside the fleet's VPC none is kept
        subprocess.run(do_fleet.ssh_argv(KEY, f"root@{self.ctl}") + ["ln -sf /dev/null /root/.ssh/fleet_known_hosts && "
                       "cat > /root/.ssh/fleet_ed25519 && chmod 600 /root/.ssh/fleet_ed25519"],
                       stdin=open(KEY), check=True, timeout=60)
        lc = self.out / "hoprd-localcluster"
        if not lc.exists():
            with open(lc, "wb") as f:
                subprocess.run(do_fleet.ssh_argv(SOURCE_KEY, SOURCE, known=str(Path.home() / ".ssh/known_hosts")) + [f"cat {LOCALCLUSTER}"],
                               stdout=f, check=True, timeout=600)
        subprocess.run(do_fleet.ssh_argv(KEY, f"root@{self.ctl}") + [f"cat > {RS}/bin/hoprd-localcluster.new && chmod +x {RS}/bin/hoprd-localcluster.new && mv {RS}/bin/hoprd-localcluster.new {RS}/bin/hoprd-localcluster"],
                       stdin=open(lc, "rb"), check=True, timeout=600)
        got = ssh(self.ctl, f"sha256sum {RS}/bin/hoprd-localcluster {RS}/bin/hoprd-4.1.3-release").split()
        if got[0] != LOCALCLUSTER_SHA or got[2] != HOPRD_SHA:
            raise RuntimeError(f"binary checksums on control: {got}")
        tar = subprocess.run(f"cd {shlex.quote(str(REPO))} && git ls-files -z | tar --null -czf - -T -", shell=True, capture_output=True, check=True).stdout
        (self.out / "repo.tgz").write_bytes(tar)
        subprocess.run(do_fleet.ssh_argv(KEY, f"root@{self.ctl}") + [f"rm -rf {RS}/gnosis_vpn-testenv && mkdir -p {RS}/gnosis_vpn-testenv {RS}/runs {RS}/logs && tar -xzf - -C {RS}/gnosis_vpn-testenv"],
                       input=tar, check=True, timeout=300)
        env = (REPO / "exploration/relay-scaling-2026-10-01-review/data/env.sh").read_text()
        ssh(self.ctl, f"cat > {RS}/env.sh", input=env)
        # every exit and client machine: the same tree and the same localcluster
        others = [do_fleet.addrs(d)[1] for k, d in ds.items() if k.startswith(("client-", "exit-"))]
        o = self.ctl_ssh_opts()
        script = " & ".join(
            f"(cat /tmp/repo.tgz | ssh {o} root@{ip} 'rm -rf {RS}/gnosis_vpn-testenv && mkdir -p {RS}/gnosis_vpn-testenv && tar -xzf - -C {RS}/gnosis_vpn-testenv' && "
            f"scp -q {o} {RS}/bin/hoprd-localcluster root@{ip}:{RS}/bin/hoprd-localcluster && "
            f"ssh {o} root@{ip} 'sha256sum {RS}/bin/hoprd-localcluster' | grep -q {LOCALCLUSTER_SHA} && echo ok {ip} || echo FAIL {ip})"
            for ip in others) + " & wait"
        subprocess.run(do_fleet.ssh_argv(KEY, f"root@{self.ctl}") + ["cat > /tmp/repo.tgz"], input=tar, check=True, timeout=300)
        res = ssh(self.ctl, script, timeout=900)
        if "FAIL" in res or res.count("ok ") != len(others):
            raise RuntimeError(f"distributing the tree: {res}")
        log("control: base specs")
        for k, d in sorted(ds.items()):
            self.specs(d["name"], do_fleet.addrs(d)[0], d)
        self.state["control_ready"] = True
        self.save()

    def write_hosts(self, tag, label):
        ds = self.droplets()
        r = self.state["relays"][label]

        def ms(group):
            ks = sorted((k for k in ds if k.startswith(group + "-") and k[len(group) + 1:].isdigit()), key=lambda k: int(k.rsplit("-", 1)[1]))
            return "\n".join(f'  {{ ssh = "root@{do_fleet.addrs(ds[k])[1]}", addr = "{do_fleet.addrs(ds[k])[1]}", name = "{k}", public = "{do_fleet.addrs(ds[k])[0]}" }},'
                             for k in ks)
        toml = f"""# relay sweep {self.name}: relay {label} ({r['size']})
[ssh]
options = "{self.ctl_ssh_opts()}"

[roles.chain]
ssh = "local"
addr = "{self.ctl_priv}"

[roles.relays]
hoprd_bin = "{RS}/bin/hoprd-4.1.3-release"
localcluster_bin = "{RS}/bin/hoprd-localcluster"
machines = [
  {{ ssh = "root@{r['private']}", addr = "{r['private']}", name = "relay-{label}", public = "{r['public']}" }},
]

[roles.exits]
hoprd_bin = "{RS}/bin/hoprd-4.1.3-release"
localcluster_bin = "{RS}/bin/hoprd-localcluster"
repo = "{RS}/gnosis_vpn-testenv"
server_image = "gnosis_vpn-server:0.7.0-upstream"
machines = [
{ms('exit')}
]

[roles.clients]
repo = "{RS}/gnosis_vpn-testenv"
machines = [
{ms('client')}
]
"""
        ssh(self.ctl, f"cat > {RS}/hosts-{tag}.toml", input=toml)

    def job(self, name, cmd, timeout):
        """cmd in the repo on control as a transient unit (survives this ssh), its log in logs/<name>.log; waits."""
        # a detached process, not a systemd-run unit: a transient unit with KillMode=process kept the ssh masters and
        # was started again by systemd minutes later, a second `up` racing the first
        unit = f"{name} (pid file {RS}/logs/{name}.pid)"
        inner = f"export HOME=/root; source {RS}/env.sh; cd {RS}/gnosis_vpn-testenv; {cmd}"
        body = f"( {inner} ) > {RS}/logs/{name}.log 2>&1; echo $? > {RS}/logs/{name}.rc"
        ssh(self.ctl, f"rm -f {RS}/logs/{name}.rc; setsid nohup bash -c {shlex.quote(body)} > /dev/null 2>&1 < /dev/null & echo $! > {RS}/logs/{name}.pid",
            timeout=60)
        t0 = time.time()
        while True:
            rc = ssh(self.ctl, f"cat {RS}/logs/{name}.rc 2>/dev/null", check=False).strip()
            if rc:
                if rc != "0":
                    tail = ssh(self.ctl, f"tail -30 {RS}/logs/{name}.log", check=False)
                    raise RuntimeError(f"job {name} exit {rc}:\n{tail}")
                return
            if time.time() - t0 > timeout:
                raise RuntimeError(f"job {name} still running after {timeout} s (unit {unit})")
            time.sleep(20)

    # -- the test ------------------------------------------------------------------------------------------------
    def vmstat(self, label, start):
        ips = [r["private"] for k, r in self.state["relays"].items() if k == label]
        ds = self.droplets()
        ips += [do_fleet.addrs(d)[1] for k, d in ds.items() if k.startswith(("client-", "exit-"))]
        o = self.ctl_ssh_opts()
        if start:
            ssh(self.ctl, " ; ".join(f"ssh {o} root@{ip} 'pkill -f \"^vmstat -n -t 1\"; setsid nohup vmstat -n -t 1 > /root/vmstat-{label}.txt 2>&1 < /dev/null &'"
                                     for ip in ips) + f"; pkill -f '^vmstat -n -t 1'; setsid nohup vmstat -n -t 1 > /root/vmstat-ctl-{label}.txt 2>&1 < /dev/null & true")
        else:
            d = self.out / "vmstat" / label
            d.mkdir(parents=True, exist_ok=True)
            names = {do_fleet.addrs(dd)[1]: dd["name"] for dd in ds.values()}
            names[self.state["relays"][label]["private"]] = self.state["relays"][label]["name"]
            for ip in ips:
                txt = ssh(self.ctl, f"ssh {o} root@{ip} 'pkill -f \"^vmstat -n -t 1\"; cat /root/vmstat-{label}.txt'", check=False)
                (d / f"{names.get(ip, ip)}.txt").write_text(txt)
            (d / f"gvpn-{self.name}-control.txt").write_text(ssh(self.ctl, f"pkill -f '^vmstat -n -t 1'; cat /root/vmstat-ctl-{label}.txt", check=False))

    def test(self, idx, label, knobs=None, run=None):
        first = not self.chain_up.is_set()
        self.write_hosts(f"test-{label}", label)
        log(f"relay {label}: bringing the shared 6 topology up ({'fresh chain' if first else 'kept chain, adopted relay'})")
        self.job(f"up-{label}", f"just multihost-up {RS}/hosts-test-{label}.toml shared {CLIENTS}" + ("" if first else " --keep-chain")
                 + " && sleep 60", timeout=3600)
        self.state["chain"] = True
        self.save()
        self.chain_up.set()
        knobs = " ".join(f"{k}={shlex.quote(v)}" for k, v in (knobs or KNOBS).items())
        run = run or f"{self.name}-{label}"
        log(f"relay {label}: test run {run}")
        self.vmstat(label, True)
        try:
            self.job(f"test-{label}", f"export {knobs}; SUITE_CELL={run} just multihost-test t34 --run-id {run}; echo pytest exit $?", timeout=6 * 3600)
        finally:
            self.vmstat(label, False)
        self.fetch(label, run)
        log(f"relay {label}: done, results in {self.out / 'runs' / run}")

    def fetch(self, label, run):
        d = self.out / "runs"
        d.mkdir(exist_ok=True)
        tgz = subprocess.run(do_fleet.ssh_argv(KEY, f"root@{self.ctl}") + [
            f"cd {RS}/runs && tar -czf - {run} -C {RS}/logs . -C {RS} hosts-test-{label}.toml"], capture_output=True, timeout=600).stdout
        (d / f"{run}.tgz").write_bytes(tgz)
        subprocess.run(["tar", "-xzf", str(d / f"{run}.tgz"), "-C", str(d)], check=False)

    # -- the whole sweep -----------------------------------------------------------------------------------------
    def smoke(self, label):
        """The pipeline end to end on one relay, small: base fleet, control, the relay, LADDER 1 2 at 10 MB. Leaves
        everything up; `run` then adopts the relay and the chain."""
        ds = self.base()
        self.setup_control(ds)
        self.chain_up = threading.Event()
        if self.state.get("chain"):
            self.chain_up.set()
        size = dict(RELAYS)[label]
        self.create_relay(label, size, start=self.chain_up.is_set())
        self.test(0, label, knobs=dict(KNOBS, T34_LADDER="1 2", T34_DOWN_BYTES="10000000"),
                  run=f"{self.name}-smoke-{int(time.time())}")

    def run(self, only=None):
        ds = self.base()
        self.setup_control(ds)
        self.chain_up = threading.Event()
        if self.state.get("chain"):
            self.chain_up.set()
        todo = [(i, lab, size) for i, (lab, size) in enumerate(RELAYS) if lab not in self.state["done"] and (not only or lab in only)]
        threads = {}

        def prepare(lab, size, start):
            try:
                self.create_relay(lab, size, start)
            except Exception as e:      # noqa: BLE001
                log(f"relay {lab}: PREPARE FAILED: {e}")
                with self.lock:
                    self.state["relays"].setdefault(lab, {})["error"] = str(e)[-500:]
                self.save()

        def launch(lab, size, start=True):
            if lab not in threads:
                threads[lab] = threading.Thread(target=prepare, args=(lab, size, start), daemon=True)
                threads[lab].start()

        for pos, (i, lab, size) in enumerate(todo):
            # the relay under test and the PREPARE after it; the first relay of a fresh chain starts with that `up`
            launch(lab, size, start=self.chain_up.is_set())
            for _, lab2, size2 in todo[pos + 1:pos + 1 + PREPARE]:
                launch(lab2, size2)
            threads[lab].join()
            if "error" in self.state["relays"].get(lab, {}):
                log(f"relay {lab}: skipped (preparation failed)")
                self.destroy_relay(lab)
                continue
            try:
                self.test(i, lab)
                self.state["done"].append(lab)
                self.save()
            except Exception as e:      # noqa: BLE001
                log(f"relay {lab}: TEST FAILED: {e}")
                with self.lock:
                    self.state["relays"][lab]["test_error"] = str(e)[-2000:]
                self.save()
            self.destroy_relay(lab)
        log("sweep finished")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--name", required=True)
    r.add_argument("--out", default="~/gnosis_vpn-testenv-logs/relay-sweep")
    r.add_argument("--only", nargs="*", help="relay labels to run (default all not yet done)")
    sm = sub.add_parser("smoke")
    sm.add_argument("--name", required=True)
    sm.add_argument("--out", default="~/gnosis_vpn-testenv-logs/relay-sweep")
    sm.add_argument("--label", default="c16")
    d = sub.add_parser("destroy")
    d.add_argument("--name", required=True)
    a = ap.parse_args()
    if a.cmd == "destroy":
        do_fleet.destroy(argparse.Namespace(name=a.name))
        return
    sw = Sweep(a.name, Path(a.out) / a.name)
    sw.smoke(a.label) if a.cmd == "smoke" else sw.run(a.only)


if __name__ == "__main__":
    main()
