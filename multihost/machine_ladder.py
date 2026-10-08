#!/usr/bin/env python3
"""T36-relay-machine-class across a ladder of relay machine classes on DigitalOcean: one relay size after another, under
one fleet whose chain, exits and clients stay.

    python3 multihost/machine_ladder.py run --name ml1 --snapshot ID --ladder digitalocean:cpu-optimized
    python3 multihost/machine_ladder.py run --name ml1 --snapshot ID --sizes c-4 c-8 --knob T36_LADDER="1 2 3"
    python3 multihost/machine_ladder.py report --name ml1                # summary.md / summary.csv of the runs so far
    python3 multihost/machine_ladder.py destroy --name ml1               # every droplet gvpn-<name>-*
    python3 multihost/machine_ladder.py calibrate digitalocean:c-4=RUNDIR [CLASS=RUNDIR ...] --source "ml1, 2026-10-08" --write

Runs on the operator's machine (the DigitalOcean token never leaves it, see do_fleet.py); state in
~/.gvpn-fleets/<name>-ladder.json, so `run` resumes where it stopped. A ladder comes from tests/machine_classes.toml
(`--ladder provider:ladder`) or is listed (`--sizes`; a size twice measures it on two droplets). Every size must have
an entry in the table (vcpus, memory_gb, cpu_model): T36-relay-machine-class holds the relay machine against it, and
scores the rungs the entry has thresholds for (none yet: recorded only, which is how a new size is calibrated).

The fleet: a control droplet (chain and suite), CLIENTS client machines and as many exit machines, all BASE_SIZE, from a
snapshot made with `do_fleet.py bake` (tools, images, hoprd and a localcluster built with
patches/hoprd-localcluster-max16.patch; `--localcluster FILE` replaces the snapshot's). Relays are made one size at a
time from plain Ubuntu (a small size's disk is below the snapshot's minimum) and deleted after their test. While a
relay is measured the next PREPARE relays are created, their specs and a CPU benchmark taken and their hoprd started on
the running chain (funded, Safe deployed, announced: `multihost.py relay-start`), so a swap only restarts the exits and
clients (`multihost.py up --keep-chain`: fresh identities, every channel to the new relay). No channel to a prepared
relay is opened before its turn: a relay any client or exit holds a channel to carries traffic, and the test pins all of
it to the relay under test.

Per relay: run id <name>-<label> (T36_MACHINE=digitalocean:<size>), 1-s vmstat on every machine for the length of the
test (vmstat/<label>/), the full specs of every droplet (specs/<droplet>.txt: DigitalOcean's record, lscpu, cpuinfo,
meminfo, dmidecode, idle steal, an openssl benchmark on one and on all threads), so an underperforming droplet can be
told from a regression afterwards.

Traps met on the first sweep (2026-10-07): DigitalOcean hands a deleted droplet's private address to the next one, so
inside the fleet no host key is kept (control's fleet_known_hosts is /dev/null); a systemd-run transient unit was
restarted by systemd minutes later and raced a second `up`, so jobs on control are detached processes (setsid nohup)."""
import argparse
import csv
import json
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "tests"))
import do_fleet  # noqa: E402
from suitelib import machineclass  # noqa: E402
from suitelib.stats import ping_summary  # noqa: E402

PROVIDER = "digitalocean"
RS = "/root/relayscale"
HOPRD = f"{RS}/bin/hoprd-4.1.3-release"           # where do_fleet.py bake puts the node binary
LOCALCLUSTER = f"{RS}/bin/hoprd-localcluster"
TEST = "T36-relay-machine-class"
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


class Ladder:
    def __init__(self, a):
        self.a = a
        self.name = a.name
        self.key = Path(a.key).expanduser()
        self.out = Path(a.out).expanduser() / a.name
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "specs").mkdir(exist_ok=True)
        self.state_path = do_fleet.STATE / f"{a.name}-ladder.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"done": [], "relays": {}}
        self.lock = threading.Lock()
        self.chain_up = threading.Event()
        if self.state.get("chain"):
            self.chain_up.set()

    def save(self):
        with self.lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, indent=2))

    def ssh(self, ip, cmd, timeout=600, check=True, input=None):
        argv = do_fleet.ssh_argv(self.key, f"root@{ip}") + [cmd]
        for attempt in range(6):        # 255: ssh itself failed (the operator's network), the command did not run
            try:
                r = subprocess.run(argv, capture_output=True, text=not isinstance(input, bytes), timeout=timeout, input=input)
            except subprocess.TimeoutExpired:
                if attempt == 5:
                    raise
                time.sleep(15)
                continue
            if r.returncode != 255:
                break
            time.sleep(15)
        if check and r.returncode:
            raise RuntimeError(f"ssh {ip} failed ({r.returncode}): {cmd[:120]}\n{str(r.stdout)[-400:]}{str(r.stderr)[-400:]}")
        return r.stdout

    def ctl_opts(self):
        return ("-i /root/.ssh/fleet_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
                "-o UserKnownHostsFile=/root/.ssh/fleet_known_hosts -o BatchMode=yes")

    # -- the ladder ----------------------------------------------------------------------------------------------
    def plan(self):
        """[(label, size)] in order: --sizes as given, else the --ladder's sizes; a repeated size gets -2, -3, ..."""
        table = machineclass.load(self.a.classes)
        if self.a.sizes:
            names = [f"{PROVIDER}:{s}" for s in self.a.sizes]
        else:
            names = machineclass.ladder(table, self.a.ladder)
        out, seen = [], {}
        for nm in names:
            provider, size, _ = machineclass.lookup(table, nm)     # every size needs its entry (T36 holds the machine to it)
            if provider != PROVIDER:
                raise SystemExit(f"{nm}: only {PROVIDER} is automated here; elsewhere bring the stack up with `just multihost-up "
                                 f"HOSTS shared N` and run T36_MACHINE={nm} just multihost-test t36 per relay")
            seen[size] = seen.get(size, 0) + 1
            out.append((size if seen[size] == 1 else f"{size}-{seen[size]}", size))
        return out

    # -- droplets ------------------------------------------------------------------------------------------------
    def droplets(self):
        return {d["name"][len(do_fleet.prefix(self.name)):]: d for d in do_fleet.fleet_droplets(self.name)}

    def base(self):
        a = self.a
        ds = self.droplets()
        if "control" not in ds:
            log(f"creating the fleet {self.name}: control + {a.clients} clients + {a.clients} exits, {a.base_size}, {a.region}")
            do_fleet.create(argparse.Namespace(name=self.name, clients=a.clients, relays=0, exits=a.clients, region=a.region, size=a.base_size,
                                               image=str(a.snapshot), pubkey=str(self.key) + ".pub", add=None, project=a.project))
        do_fleet.wait(argparse.Namespace(name=self.name, key=str(self.key), timeout=1500))
        ds = self.droplets()
        self.ctl, self.ctl_priv = do_fleet.addrs(ds["control"])[:2]
        return ds

    def create_relay(self, label, size, start=True):
        """The relay droplet (plain Ubuntu 24.04), its first boot, specs and benchmark, the binaries from control (checked
        by sha256), its hoprd on the chain."""
        name = f"relay-{label}"
        full = do_fleet.prefix(self.name) + name
        if self.state["relays"].get(label, {}).get("started"):
            return
        if name not in self.droplets():
            log(f"relay {label}: creating {full} ({size})")
            body = {"names": [full], "region": self.a.region, "size": size, "image": "ubuntu-24-04-x64", "ipv6": False, "monitoring": False,
                    "user_data": do_fleet.USER_DATA.format(pubkey=Path(str(self.key) + ".pub").read_text().strip())}
            d = do_fleet.api("POST", "/droplets", body)["droplets"][0]
            do_fleet.assign_to_project(self.a.project, [d["id"]])
        t0 = time.time()
        while True:
            d = self.droplets().get(name)
            if d and d["status"] == "active" and do_fleet.addrs(d)[0]:
                ok = subprocess.run(do_fleet.ssh_argv(self.key, f"root@{do_fleet.addrs(d)[0]}") + [f"test -f {do_fleet.READY}"],
                                    capture_output=True, timeout=30).returncode == 0
                if ok:
                    break
            if time.time() - t0 > 1500:
                raise RuntimeError(f"relay {label} not ready after 1500 s")
            time.sleep(15)
        pub, priv = do_fleet.addrs(d)[:2]
        with self.lock:
            self.state["relays"][label] = {"size": size, "id": d["id"], "name": full, "public": pub, "private": priv,
                                           "ready": time.strftime("%FT%TZ", time.gmtime())}
        self.save()
        log(f"relay {label}: {full} up after {int(time.time() - t0)} s ({pub} / {priv}); specs and benchmark")
        if not (self.out / "specs" / f"{full}.txt").exists():
            self.specs(full, pub, d)
        o = self.ctl_opts()
        want = self.state["sha"]
        have = self.ssh(self.ctl, f"ssh {o} root@{priv} 'sha256sum {HOPRD} {LOCALCLUSTER} 2>/dev/null'", check=False)
        if not all(v in have for v in want.values()):
            self.ssh(self.ctl, f"ssh {o} root@{priv} 'mkdir -p {RS}/bin' && scp -q {o} {HOPRD} {LOCALCLUSTER} root@{priv}:{RS}/bin/", timeout=600)
            have = self.ssh(self.ctl, f"ssh {o} root@{priv} 'sha256sum {HOPRD} {LOCALCLUSTER}'")
            if not all(v in have for v in want.values()):
                raise RuntimeError(f"relay {label}: binaries' checksums {have}, want {want}")
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
        if not r or "id" not in r:
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

    def specs(self, full, pub, d):
        txt = self.ssh(pub, SPECS, timeout=300, check=False)
        api = {k: d.get(k) for k in ("id", "name", "size_slug", "vcpus", "memory", "disk", "created_at", "image", "kernel", "features")}
        api["region"] = d["region"]["slug"]
        api["size"] = d.get("size")
        (self.out / "specs" / f"{full}.txt").write_text("### digitalocean api\n" + json.dumps(api, indent=2) + "\n" + txt)

    # -- control -------------------------------------------------------------------------------------------------
    def setup_control(self, ds):
        if self.state.get("control_ready"):
            return
        log("control: fleet key, binaries, repo tree, env")
        # relays come and go and DigitalOcean hands a deleted relay's private address to the next one: a kept host key
        # then fails every ssh to it ("REMOTE HOST IDENTIFICATION HAS CHANGED"); inside the fleet's VPC none is kept
        self.ssh(self.ctl, "ln -sf /dev/null /root/.ssh/fleet_known_hosts && cat > /root/.ssh/fleet_ed25519 && chmod 600 /root/.ssh/fleet_ed25519",
                 input=self.key.read_text())
        if self.a.localcluster:
            self.ssh(self.ctl, f"mkdir -p {RS}/bin && cat > {LOCALCLUSTER}.new && chmod +x {LOCALCLUSTER}.new && mv {LOCALCLUSTER}.new {LOCALCLUSTER}",
                     input=Path(self.a.localcluster).expanduser().read_bytes())
        got = self.ssh(self.ctl, f"sha256sum {HOPRD} {LOCALCLUSTER}").split()
        self.state["sha"] = {"hoprd": got[0], "localcluster": got[2]}
        imgs = self.ssh(self.ctl, "docker images --format '{{.Repository}}:{{.Tag}}'").split()

        def image(opt, role):
            found = [i for i in imgs if i.startswith(f"gnosis_vpn-{role}:")]
            if opt or len(found) == 1:
                return opt or found[0]
            raise SystemExit(f"control has {found or 'no'} gnosis_vpn-{role} image(s); name one with --{role}-image")

        self.state["client_image"] = image(self.a.client_image, "client")
        self.state["server_image"] = image(self.a.server_image, "server")
        # this working tree, uncommitted changes and new files included (the operator runs what they have)
        tar = subprocess.run(f"cd {shlex.quote(str(REPO))} && git ls-files -co --exclude-standard -z | tar --null -czf - -T -", shell=True,
                             capture_output=True, check=True).stdout
        self.ssh(self.ctl, "cat > /tmp/repo.tgz", input=tar, timeout=300)
        self.ssh(self.ctl, f"rm -rf {RS}/gnosis_vpn-testenv && mkdir -p {RS}/gnosis_vpn-testenv {RS}/runs {RS}/logs && "
                           f"tar -xzf /tmp/repo.tgz -C {RS}/gnosis_vpn-testenv", timeout=300)
        env = (f"export PATH=/usr/local/bin:$PATH\n"
               f"export HOPRD_DIR={RS}/bin GVPN_CLIENT_DIR=/nonexistent GVPN_SERVER_DIR=/nonexistent\n"
               f"export HOPRD_BIN={HOPRD} LOCALCLUSTER_BIN={LOCALCLUSTER}\n"
               f"export CLIENT_IMAGE={self.state['client_image']} SERVER_IMAGE={self.state['server_image']}\n"
               'export CLUSTER_ENV="" CLIENT_EXTRA_ENV="" CLIENT_EXTRA_ARGS="" CLIENT_SYSCTL="" CLUSTER_LATENCY=""\n'
               'export CLIENT_LOG_LEVEL="warn,gnosis_vpn_root=debug,gnosis_vpn_lib=debug,gnosis_vpn_worker=debug"\n'
               f"export CLIENT_AUTOSTART=48h SUITE_OUT_DIR={RS}/runs\n")
        self.ssh(self.ctl, f"cat > {RS}/env.sh", input=env)
        # every exit and client machine: the same tree and the same localcluster
        others = [do_fleet.addrs(d)[1] for k, d in ds.items() if k.startswith(("client-", "exit-"))]
        o = self.ctl_opts()
        script = " & ".join(
            f"(cat /tmp/repo.tgz | ssh {o} root@{ip} 'rm -rf {RS}/gnosis_vpn-testenv && mkdir -p {RS}/gnosis_vpn-testenv {RS}/bin && tar -xzf - -C {RS}/gnosis_vpn-testenv' && "
            f"scp -q {o} {LOCALCLUSTER} root@{ip}:{LOCALCLUSTER} && "
            f"ssh {o} root@{ip} 'sha256sum {LOCALCLUSTER}' | grep -q {self.state['sha']['localcluster']} && echo ok {ip} || echo FAIL {ip})"
            for ip in others) + " & wait"
        res = self.ssh(self.ctl, script, timeout=900)
        if "FAIL" in res or res.count("ok ") != len(others):
            raise RuntimeError(f"distributing the tree: {res}")
        log("control: specs of the fleet")
        for _, d in sorted(ds.items()):
            self.specs(d["name"], do_fleet.addrs(d)[0], d)
        self.state["control_ready"] = True
        self.save()

    def write_hosts(self, tag, label):
        ds = self.droplets()
        r = self.state["relays"][label]

        def ms(group):
            ks = sorted((k for k in ds if k.startswith(group + "-") and k[len(group) + 1:].isdigit()), key=lambda k: int(k.rsplit("-", 1)[1]))
            return "\n".join(f'  {{ ssh = "root@{do_fleet.addrs(ds[k])[1]}", addr = "{do_fleet.addrs(ds[k])[1]}", name = "{k}", '
                             f'public = "{do_fleet.addrs(ds[k])[0]}" }},' for k in ks)
        toml = f"""# machine ladder {self.name}: relay {label} ({r['size']})
[ssh]
options = "{self.ctl_opts()}"

[roles.chain]
ssh = "local"
addr = "{self.ctl_priv}"

[roles.relays]
hoprd_bin = "{HOPRD}"
localcluster_bin = "{LOCALCLUSTER}"
machines = [
  {{ ssh = "root@{r['private']}", addr = "{r['private']}", name = "relay-{label}", public = "{r['public']}" }},
]

[roles.exits]
hoprd_bin = "{HOPRD}"
localcluster_bin = "{LOCALCLUSTER}"
repo = "{RS}/gnosis_vpn-testenv"
server_image = "{self.state['server_image']}"
machines = [
{ms('exit')}
]

[roles.clients]
repo = "{RS}/gnosis_vpn-testenv"
machines = [
{ms('client')}
]
"""
        self.ssh(self.ctl, f"cat > {RS}/hosts-{tag}.toml", input=toml)

    def job(self, name, cmd, timeout):
        """cmd in the repo on control as a detached process (survives this ssh), its log in logs/<name>.log; waits."""
        # not a systemd-run unit: a transient unit with KillMode=process was started again by systemd minutes later,
        # a second `up` racing the first
        inner = f"export HOME=/root; source {RS}/env.sh; cd {RS}/gnosis_vpn-testenv; {cmd}"
        body = f"( {inner} ) > {RS}/logs/{name}.log 2>&1; echo $? > {RS}/logs/{name}.rc"
        self.ssh(self.ctl, f"rm -f {RS}/logs/{name}.rc; setsid nohup bash -c {shlex.quote(body)} > /dev/null 2>&1 < /dev/null & "
                           f"echo $! > {RS}/logs/{name}.pid", timeout=60)
        t0 = time.time()
        while True:
            rc = self.ssh(self.ctl, f"cat {RS}/logs/{name}.rc 2>/dev/null", check=False).strip()
            if rc:
                if rc != "0":
                    raise RuntimeError(f"job {name} exit {rc}:\n{self.ssh(self.ctl, f'tail -30 {RS}/logs/{name}.log', check=False)}")
                return
            if time.time() - t0 > timeout:
                raise RuntimeError(f"job {name} still running after {timeout} s (pid in {RS}/logs/{name}.pid)")
            time.sleep(20)

    # -- one relay -----------------------------------------------------------------------------------------------
    def vmstat(self, label, start):
        ds = self.droplets()
        ips = [self.state["relays"][label]["private"]] + [do_fleet.addrs(d)[1] for k, d in ds.items() if k.startswith(("client-", "exit-"))]
        o = self.ctl_opts()
        pat = "'^vmstat -n -t 1'"
        if start:
            self.ssh(self.ctl, " ; ".join(f"ssh {o} root@{ip} \"pkill -f {pat}; setsid nohup vmstat -n -t 1 > /root/vmstat-{label}.txt 2>&1 < /dev/null &\""
                                          for ip in ips) + f"; pkill -f {pat}; setsid nohup vmstat -n -t 1 > /root/vmstat-ctl-{label}.txt 2>&1 < /dev/null & true")
            return
        d = self.out / "vmstat" / label
        d.mkdir(parents=True, exist_ok=True)
        names = {do_fleet.addrs(dd)[1]: dd["name"] for dd in ds.values()}
        names[self.state["relays"][label]["private"]] = self.state["relays"][label]["name"]
        for ip in ips:
            txt = self.ssh(self.ctl, f"ssh {o} root@{ip} \"pkill -f {pat}; cat /root/vmstat-{label}.txt\"", check=False)
            (d / f"{names.get(ip, ip)}.txt").write_text(txt)
        (d / f"{do_fleet.prefix(self.name)}control.txt").write_text(self.ssh(self.ctl, f"pkill -f {pat}; cat /root/vmstat-ctl-{label}.txt", check=False))

    def test(self, label, size):
        first = not self.chain_up.is_set()
        self.write_hosts(f"test-{label}", label)
        log(f"relay {label}: bringing the shared {self.a.clients} topology up ({'fresh chain' if first else 'kept chain, adopted relay'})")
        self.job(f"up-{label}", f"just multihost-up {RS}/hosts-test-{label}.toml shared {self.a.clients}" + ("" if first else " --keep-chain")
                 + " && sleep 60", timeout=3600)
        self.state["chain"] = True
        self.save()
        self.chain_up.set()
        knobs = dict(kv.split("=", 1) for kv in self.a.knob)
        knobs["T36_MACHINE"] = f"{PROVIDER}:{size}"
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in knobs.items())
        run = f"{self.name}-{label}"
        log(f"relay {label}: {TEST}, run {run} ({env})")
        self.vmstat(label, True)
        try:
            self.job(f"test-{label}", f"export {env}; SUITE_CELL={run} just multihost-test t36 --run-id {run}; echo pytest exit $?", timeout=6 * 3600)
        finally:
            self.vmstat(label, False)
        self.fetch(label, run)
        log(f"relay {label}: done, results in {self.out / 'runs' / run}")

    def fetch(self, label, run):
        d = self.out / "runs"
        d.mkdir(exist_ok=True)
        tgz = subprocess.run(do_fleet.ssh_argv(self.key, f"root@{self.ctl}") + [
            f"cd {RS}/runs && tar -czf - {run} -C {RS}/logs . -C {RS} hosts-test-{label}.toml"], capture_output=True, timeout=600).stdout
        (d / f"{run}.tgz").write_bytes(tgz)
        subprocess.run(["tar", "-xzf", str(d / f"{run}.tgz"), "-C", str(d)], check=False)

    # -- the whole ladder ----------------------------------------------------------------------------------------
    def run(self):
        plan = self.plan()
        ds = self.base()
        self.setup_control(ds)
        todo = [(lab, size) for lab, size in plan if lab not in self.state["done"]]
        log(f"ladder {self.name}: {', '.join(lab for lab, _ in todo) or 'nothing left'}")
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

        for pos, (lab, size) in enumerate(todo):
            # the relay under test and the PREPARE after it; the first relay of a fresh chain starts with that `up`
            launch(lab, size, start=self.chain_up.is_set())
            for lab2, size2 in todo[pos + 1:pos + 1 + self.a.prepare]:
                launch(lab2, size2)
            threads[lab].join()
            if "error" in self.state["relays"].get(lab, {}):
                log(f"relay {lab}: skipped (preparation failed)")
                self.destroy_relay(lab)
                continue
            try:
                self.test(lab, size)
                self.state["done"].append(lab)
                self.save()
            except Exception as e:      # noqa: BLE001
                log(f"relay {lab}: TEST FAILED: {e}")
                with self.lock:
                    self.state["relays"][lab]["test_error"] = str(e)[-2000:]
                self.save()
            self.destroy_relay(lab)
        log(f"ladder finished; the fleet is still up: `{Path(sys.argv[0]).name} destroy --name {self.name}` after fetching what you need")
        report(self.out)


# -- reading runs ----------------------------------------------------------------------------------------------------
def read_run(path):
    """(machine class or None, {n: rung row}, stack info) of one run directory of T34-single-relay-scaling or
    T36-relay-machine-class; a rung row lacking down_ping gets it from the run's probe files."""
    path = Path(path).expanduser()
    rows = [json.loads(x) for x in (path / "rows.jsonl").read_text().splitlines() if x.strip()]
    rows = [r for r in rows if r.get("test") in ("T34-single-relay-scaling", TEST)]
    cls = next((r["machine"] for r in rows if r.get("kind") == "machine_class"), None)
    stack = next((r.get("stack") for r in rows if r.get("kind") == "topology"), {}) or {}
    rungs = {}
    for r in rows:
        if r.get("kind") != "rung":
            continue
        if r.get("down_ping") is None:
            tag = r["test"].split("-")[0].lower()
            probes = [json.loads(f.read_text()) for f in sorted(path.glob(f"{tag}-n{r['n']}-down-*.json"))]
            r["down_ping"] = ping_summary(probes) if probes else None
        rungs[r["n"]] = r
    knobs = None
    console = path / "console.log"
    if console.exists():
        for line in console.read_text(errors="replace").splitlines():
            if " knobs: " in line:
                knobs = dict(w.split("=", 1) for w in line.split(" knobs: ", 1)[1].split(" ") if "=" in w)
                break
    return cls, rungs, stack, knobs


def report(out):
    """summary.csv and summary.md of every run under out/runs: per relay and rung, the aggregate, the ping, the relay's
    CPU and the class's thresholds."""
    out = Path(out).expanduser()
    table = machineclass.load(None)
    summ = []
    for run in sorted((out / "runs").glob("*/rows.jsonl")):
        cls, rungs, _, _ = read_run(run.parent)
        try:
            entry = machineclass.lookup(table, cls)[2] if cls else {}
        except KeyError:
            entry = {}
        for n, r in sorted(rungs.items()):
            agg_min, ping_max = machineclass.thresholds(entry, n)
            p = r.get("down_ping") or {}
            proc = [v for v in (r.get("ut_process_cpu_down") or {}).values() if v is not None]
            summ.append({"run": run.parent.name, "class": cls or "", "clients": n, "agg_mbit": r.get("down_agg_mbit"), "agg_min_mbit": agg_min,
                         "per_client_mean_mbit": r.get("down_avg_mbit"), "complete": n - (r.get("incomplete_down") or 0),
                         "ping_p50_ms": p.get("p50_ms"), "ping_p50_max_ms": ping_max, "ping_p95_ms": p.get("p95_ms"),
                         "ping_loss_pct": p.get("loss_pct"), "relay_machine_cpu_pct": r.get("ut_machine_cpu_down"),
                         "relay_process_cpu_pct": proc[0] if proc else None, "reconnects": r.get("reconnects")})
    if not summ:
        log(f"no runs under {out / 'runs'}")
        return
    with open(out / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summ[0]))
        w.writeheader()
        w.writerows(summ)
    ns = sorted({s["clients"] for s in summ})
    runs = list(dict.fromkeys(s["run"] for s in summ))
    get = {(s["run"], s["clients"]): s for s in summ}
    cls = {s["run"]: s["class"] for s in summ}

    def grid(title, cell):
        md = ["", title, "", "| run | class | " + " | ".join(f"n={n}" for n in ns) + " |", "|---|---|" + "---|" * len(ns)]
        md += [f"| {r} | {cls[r]} | " + " | ".join(cell(get[(r, n)]) if (r, n) in get else "" for n in ns) + " |" for r in runs]
        return md

    md = grid("Aggregate download, Mbit/s (threshold agg_min_mbit in brackets; ! below it)",
              lambda s: f"{s['agg_mbit']} ({s['agg_min_mbit'] if s['agg_min_mbit'] is not None else '-'})"
              + (" !" if s["agg_min_mbit"] is not None and (s["agg_mbit"] or 0) < s["agg_min_mbit"] else ""))
    md += grid("Ping median during the download, ms (threshold ping_p50_max_ms in brackets; ! above it), loss %",
               lambda s: f"{s['ping_p50_ms']} ({s['ping_p50_max_ms'] if s['ping_p50_max_ms'] is not None else '-'})"
               + (" !" if s["ping_p50_max_ms"] is not None and s["ping_p50_ms"] is not None and s["ping_p50_ms"] > s["ping_p50_max_ms"] else "")
               + f", {s['ping_loss_pct']} %")
    md += grid("Relay machine CPU % (all cores) / hoprd process % of one core", lambda s: f"{s['relay_machine_cpu_pct']} / {s['relay_process_cpu_pct']}")
    (out / "summary.md").write_text("\n".join(md[1:]) + "\n")
    print("\n".join(md[1:]))


def calibrate(a):
    """The table with the given classes' measurements and thresholds replaced by those of the given runs (several runs
    of one class: their lists side by side, the thresholds from the worst). Refuses a run whose knobs differ from the
    table's [calibration] knobs."""
    path = Path(a.classes).expanduser() if a.classes else machineclass.DEFAULT
    table = machineclass.load(path)
    cal = table.setdefault("calibration", {})
    want = cal.get("knobs", {})
    by_class, stacks = {}, []
    for spec in a.runs:
        name, _, run = spec.partition("=")
        if not run:
            raise SystemExit(f"{spec}: give CLASS=RUNDIR")
        machineclass.lookup(table, name)
        cls, rungs, stack, knobs = read_run(run)
        if cls and cls != name:
            raise SystemExit(f"{run} was run as {cls}, not {name}")
        off = [f"{k}={(knobs or {}).get(k)} (table: {v})" for k, v in want.items() if str((knobs or {}).get(k)) != str(v)]
        if off:
            raise SystemExit(f"{run}: knobs differ from the table's calibration knobs: {'; '.join(off)}")
        ns = []
        for n in range(1, max(rungs, default=0) + 1):            # contiguous rungs from n=1 only
            if n not in rungs:
                break
            ns.append(rungs[n])
        if not ns:
            raise SystemExit(f"{run}: no rung measured")
        aggs = [round(r["down_agg_mbit"], 2) for r in ns]
        pings = []
        for r in ns:
            if not r.get("down_ping") or r["down_ping"].get("p50_ms") is None:
                break
            pings.append(round(r["down_ping"]["p50_ms"], 1))
        by_class.setdefault(name, []).append((Path(run).name, aggs, pings))
        stacks.append(stack)
    for name, runs in by_class.items():
        provider, size, entry = machineclass.lookup(table, name)
        agg, ping = machineclass.derive([r[1] for r in runs], [r[2] for r in runs], cal["floor_pct"], cal["ping_mult"])
        entry.update(runs=[r[0] for r in runs], measured_agg_mbit=[r[1] for r in runs], measured_ping_p50_ms=[r[2] for r in runs],
                     agg_min_mbit=agg, ping_p50_max_ms=ping)
        log(f"{name}: agg_min_mbit {agg}, ping_p50_max_ms {ping} from {', '.join(r[0] for r in runs)}")
    if a.source:
        cal["source"] = a.source
    s = next((x for x in stacks if x), {})
    if s:
        cal["hoprd"] = (s.get("hoprd") or {}).get("relay", cal.get("hoprd"))
        cal["client"] = s.get("client", cal.get("client"))
        cal["client_image"] = s.get("client_image", cal.get("client_image"))
    text = machineclass.render(table)
    if machineclass.tomllib.loads(text) != table:
        raise SystemExit("the rendered table does not read back as written (a bug in machineclass.render)")
    if a.write:
        path.write_text(text)
        log(f"wrote {path}")
    else:
        print(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="create the fleet if needed and run T36-relay-machine-class on every relay size of the ladder")
    r.add_argument("--name", required=True)
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--ladder", help="provider:ladder from the table, e.g. digitalocean:cpu-optimized")
    g.add_argument("--sizes", nargs="+", help="DigitalOcean size slugs in order (a slug twice: two droplets)")
    r.add_argument("--snapshot", required=True, help="the base image of control, clients and exits (do_fleet.py bake)")
    r.add_argument("--localcluster", help="a hoprd-localcluster binary built with patches/hoprd-localcluster-max16.patch, replacing the snapshot's")
    r.add_argument("--client-image", help="default: the one gnosis_vpn-client image on the snapshot")
    r.add_argument("--server-image", help="default: the one gnosis_vpn-server image on the snapshot")
    r.add_argument("--clients", type=int, default=6, help="clients and exits, one machine each (the table's calibration: 6)")
    r.add_argument("--base-size", default="c-8", help="clients, exits and control (the table's calibration: c-8)")
    r.add_argument("--region", default="lon1", help="where every size of the ladder is offered (the table's calibration: lon1)")
    r.add_argument("--prepare", type=int, default=3, help="relays made and started ahead of the one under test")
    r.add_argument("--knob", action="append", default=[], metavar="T36_VAR=VALUE", help="passed to the test (T36_MACHINE is set per relay)")
    rp = sub.add_parser("report", help="summary.md and summary.csv of the ladder's runs so far")
    cb = sub.add_parser("calibrate", help="rewrite the table's measurements and thresholds of the given classes from runs")
    cb.add_argument("runs", nargs="+", metavar="CLASS=RUNDIR")
    cb.add_argument("--source", help="what the calibration was measured on (the table's calibration.source)")
    cb.add_argument("--write", action="store_true", help="write the table (default: print it)")
    d = sub.add_parser("destroy", help="delete every droplet of the fleet")
    for p in (rp, d):
        p.add_argument("--name", required=True)
    for p in (r, rp):
        p.add_argument("--out", default="~/gnosis_vpn-testenv-logs/machine-ladder")
    for p in (r, cb):
        p.add_argument("--classes", help="the machine class table (default tests/machine_classes.toml)")
    r.add_argument("--key", default="~/.ssh/do_testenv_fleet_ed25519", help="the fleet's ssh key (private; .pub next to it)")
    r.add_argument("--project", default=do_fleet.PROJECT)
    a = ap.parse_args()
    if a.cmd == "destroy":
        do_fleet.destroy(argparse.Namespace(name=a.name))
    elif a.cmd == "report":
        report(Path(a.out) / a.name)
    elif a.cmd == "calibrate":
        calibrate(a)
    else:
        Ladder(a).run()


if __name__ == "__main__":
    main()
