#!/usr/bin/env python3
"""T36-relay-machine-class across a ladder of relay machine classes on DigitalOcean: one relay size after another, under
one fleet whose chain, exits and clients stay.

    python3 multihost/machine_ladder.py run --name ml1 --snapshot ID --ladder digitalocean:cpu-optimized
    python3 multihost/machine_ladder.py run --name ml1 --snapshot ID --sizes c-4 c-8 --knob T36_LADDER="1 2 3"
    python3 multihost/machine_ladder.py report --name ml1                # summary.md / summary.csv of the runs so far
    python3 multihost/machine_ladder.py destroy --name ml1               # every droplet gvpn-<name>-*
    python3 multihost/machine_ladder.py calibrate digitalocean:c-4=RUNDIR [CLASS=RUNDIR ...] --source "ml1, 2026-10-08" --write
    multihost/aws_python multihost/machine_ladder.py run --provider aws --name al1 --snapshot AMI --ladder aws:physical-cores \
        --swap resize --base-size c7a.xlarge --region eu-north-1        # AWS EC2 (aws_fleet.py), one relay resized in place

On AWS (--provider aws, aws_fleet.py) the same fleet runs on EC2, and --swap resize replaces the per-size relay machines
with one relay that is stopped, given the next instance type and started again: it keeps its private address and
disk, so its node (identity, announced address, every channel to it) carries over and only its hoprd restarts; exits
and clients are not touched. Measured on the smoke fleet 2026-10-08: 176 s per swap (43 s of it the resize) against
441 s for the `up` of a fresh stack. Moving a relay's identity to a new machine with a new address works on the relay
side (hoprd re-announces itself on start, the exits follow at once) but the clients (0.96.4) did not route through it
again until they were restarted: on AWS use --swap resize.

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
import math
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "tests"))
import do_fleet  # noqa: E402
import providers  # noqa: E402
from suitelib import machineclass  # noqa: E402
from suitelib.stats import ping_summary  # noqa: E402

RS = "/root/relayscale"
HOPRD = f"{RS}/bin/hoprd-4.1.3-release"           # where do_fleet.py bake puts the node binary
LOCALCLUSTER = f"{RS}/bin/hoprd-localcluster"
TEST = "T36-relay-machine-class"
SPECS = r"""set +e
echo "### date"; date -u +%FT%TZ
echo "### droplet"; for k in id hostname region; do printf '%s=' $k; curl -sf -m 3 http://169.254.169.254/metadata/v1/$k; echo; done
echo "### ec2"; T=$(curl -sf -m 3 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token) && for k in instance-id instance-type placement/availability-zone; do printf '%s=' $k; curl -sf -m 3 -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/$k; echo; done
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
W=$(ip route show default | awk '{print $5; exit}')
echo "### net"; ip -br addr; ethtool -i $W 2>/dev/null; ethtool $W 2>/dev/null | grep -i speed; ethtool -l $W 2>/dev/null
echo "### sysctl"; sysctl net.core.rmem_max net.core.wmem_max net.core.rmem_default net.core.netdev_max_backlog
echo "### loadavg"; cat /proc/loadavg
echo "### vmstat 1 10 (idle steal)"; vmstat -n -t 1 10
echo "### openssl speed sha256, 1 thread"; openssl speed -seconds 3 sha256 2>/dev/null | tail -2
echo "### openssl speed sha256, $(nproc) threads"; openssl speed -seconds 3 -multi $(nproc) sha256 2>/dev/null | tail -2
echo "### openssl speed aes-128-gcm, 1 thread"; openssl speed -seconds 3 -evp aes-128-gcm 2>/dev/null | tail -2
"""


RELAY_DATA = "/tmp/hopr-mh-relays"            # tests/multihost.py DATA["relays"]
# Before a resize: every hoprd of the relay's localcluster, as it runs (argv, environment, cwd, where its output goes),
# so the same nodes start again afterwards with their identity, database and address
SAVE_HOPRD = r"""
import json, os
out = []
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        argv = [x.decode() for x in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[:-1]]
        raw = open(f"/proc/{pid}/environ", "rb").read()
    except OSError:
        continue
    if not argv or os.path.basename(argv[0]) != os.path.basename(HOPRD) or (DATA not in " ".join(argv) and DATA.encode() not in raw):
        continue
    env = dict(x.decode(errors="replace").split("=", 1) for x in raw.split(b"\0") if b"=" in x)
    fd1 = os.readlink(f"/proc/{pid}/fd/1")
    out.append({"pid": int(pid), "argv": argv, "env": env, "cwd": os.readlink(f"/proc/{pid}/cwd"), "stdout": fd1})
json.dump(out, open("/root/hoprd-relaunch.json", "w"), indent=1)
print(len(out))
"""
RELAUNCH_HOPRD = r"""
import json, os, subprocess
for i, p in enumerate(json.load(open("/root/hoprd-relaunch.json"))):
    log = p["stdout"] if p["stdout"].startswith("/") and not p["stdout"].startswith("/dev/") else f"{DATA}/logs/hoprd-relaunch-{i}.log"
    os.makedirs(os.path.dirname(log), exist_ok=True)
    proc = subprocess.Popen(p["argv"], env=p["env"], cwd=p["cwd"], stdout=open(log, "ab"), stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    port = p["argv"][p["argv"].index("--apiPort") + 1] if "--apiPort" in p["argv"] else ""
    print("relaunched", p["argv"][0], log, "apiPort", port, "pid", proc.pid)
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
        self.prov = providers.get(a.provider)
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
            names = [f"{self.prov.name}:{s}" for s in self.a.sizes]
        else:
            names = machineclass.ladder(table, self.a.ladder)
        out, seen = [], {}
        for nm in names:
            provider, size, _ = machineclass.lookup(table, nm)     # every size needs its entry (T36 holds the machine to it)
            if provider != self.prov.name:
                raise SystemExit(f"{nm}: this ladder runs on {self.prov.name} (--provider); elsewhere bring the stack up with `just multihost-up "
                                 f"HOSTS shared N` and run T36_MACHINE={nm} just multihost-test t36 per relay")
            seen[size] = seen.get(size, 0) + 1
            out.append((size if seen[size] == 1 else f"{size}-{seen[size]}", size))
        return out

    # -- machines ------------------------------------------------------------------------------------------------
    def droplets(self):
        return self.prov.machines(self.name)

    def base(self):
        a = self.a
        ds = self.droplets()
        if "control" not in ds:
            log(f"creating the fleet {self.name} on {self.prov.name}: control + {a.clients} clients + {a.clients} exits, {a.base_size}, {a.region}")
            for attempt in range(20):     # a raised quota reaches EC2 minutes after the request shows approved
                try:
                    self.prov.create_fleet(self.name, a.clients, a.clients, a.base_size, a.snapshot, str(self.key) + ".pub", a.project, a.region,
                                           control_size=a.control_size)
                    break
                except SystemExit as e:
                    if attempt == 19 or "VcpuLimitExceeded" not in str(e):
                        raise
                    log(f"fleet: the vCPU limit refused it ({str(e)[-120:]}); everything made was removed, retrying in 120 s")
                    time.sleep(120)
        else:
            # a fleet made for fewer clients grows to --clients: the missing clients and exits, then a fresh `up`
            missing = [f"{role}-{i}" for role in ("client", "exit") for i in range(1, a.clients + 1) if f"{role}-{i}" not in ds]
            if missing:
                log(f"fleet {self.name}: adding {len(missing)} machine(s) ({a.base_size}): {', '.join(missing)}")
                for m in missing:
                    self.prov.create_one(self.name, m, a.base_size, a.snapshot, str(self.key) + ".pub", a.project, a.region)
                with self.lock:
                    self.state["chain"] = False
                    self.state["control_ready"] = False      # control's specs and known machines include the new ones
                    for r in self.state["relays"].values():
                        r.pop("started", None)
                self.save()
                self.chain_up.clear()
        self.prov.wait(self.name, str(self.key), timeout=1500)
        ds = {k: d for k, d in self.droplets().items() if not k.startswith("relay-")}
        self.ctl, self.ctl_priv = ds["control"]["public"], ds["control"]["private"]
        return ds

    def ready(self, ip):
        return subprocess.run(do_fleet.ssh_argv(self.key, f"root@{ip}") + [f"test -f {do_fleet.READY}"],
                              capture_output=True, timeout=40).returncode == 0

    def create_relay(self, label, size, start=True, member=None):
        """The relay droplet (plain Ubuntu 24.04), its first boot, specs and benchmark, the binaries from control (checked
        by sha256), its hoprd on the chain."""
        name = member or f"relay-{label}"
        full = do_fleet.prefix(self.name) + name
        if self.state["relays"].get(label, {}).get("started"):
            return
        have = self.droplets().get(name)
        if have and have["size"] != size and self.prov.can_resize:
            # the hot relay of a fleet that grew (fresh chain, nothing to carry over): resized here, started by the `up`
            log(f"relay {label}: {full} is {have['size']}; resizing to {size}")
            self.prov.resize(self.name, name, size)
        if not have:
            image = self.a.relay_image or self.prov.relay_image_default or self.a.snapshot
            log(f"relay {label}: creating {full} ({size}, image {image})")
            for attempt in range(40):       # a new account's quota or a size's capacity: retried for 40 x 90 s
                try:
                    self.prov.create_one(self.name, name, size, image, str(self.key) + ".pub", self.a.project, self.a.region)
                    break
                except SystemExit as e:
                    if attempt == 39 or not any(w in str(e) for w in ("Capacity", "VcpuLimitExceeded", "InstanceLimitExceeded", "429")):
                        raise RuntimeError(str(e))
                    log(f"relay {label}: {str(e)[:160]}; retrying in 90 s")
                    time.sleep(90)
        d, waited = providers.ready_wait(self.prov, self.name, name, self.key, self.ready)
        t0 = time.time() - waited
        pub, priv = d["public"], d["private"]
        with self.lock:
            self.state["relays"][label] = {"size": size, "id": d["id"], "name": full, "member": name, "public": pub, "private": priv,
                                           "ready": time.strftime("%FT%TZ", time.gmtime())}
        self.save()
        log(f"relay {label}: {full} up after {int(time.time() - t0)} s ({pub} / {priv}); specs and benchmark")
        if self.a.swap == "resize":
            # the relay's data dir (/tmp/hopr-mh-relays) must survive the stop/start of a resize: Ubuntu empties /tmp at boot
            self.ssh(pub, "printf 'd /tmp 1777 root root -\\n' > /etc/tmpfiles.d/tmp.conf")
        if not (self.out / "specs" / f"{full}.txt").exists():
            self.specs(full, pub, d)
        o = self.ctl_opts()
        want = self.state["sha"]
        have = self.ssh(self.ctl, f"ssh {o} root@{priv} 'sha256sum {HOPRD} {LOCALCLUSTER} 2>/dev/null'", check=False)
        if not all(v in have for v in want.values()):
            # to new names, then renamed over the old ones: a hoprd or localcluster still running there makes its binary busy
            self.ssh(self.ctl, f"ssh {o} root@{priv} 'mkdir -p {RS}/bin' && scp -q {o} {HOPRD} root@{priv}:{HOPRD}.new && "
                               f"scp -q {o} {LOCALCLUSTER} root@{priv}:{LOCALCLUSTER}.new && "
                               f"ssh {o} root@{priv} 'mv -f {HOPRD}.new {HOPRD} && mv -f {LOCALCLUSTER}.new {LOCALCLUSTER}'", timeout=600)
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
        self.prov.delete(r["id"])
        with self.lock:
            r["deleted"] = time.strftime("%FT%TZ", time.gmtime())
        self.save()
        log(f"relay {label}: droplet {r['name']} deleted")

    def specs(self, full, pub, d):
        txt = self.ssh(pub, SPECS, timeout=300, check=False)
        (self.out / "specs" / f"{full}.txt").write_text(f"### {self.prov.name} api\n" + json.dumps(self.prov.record(d), indent=2) + "\n" + txt)

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
        others = [d["private"] for k, d in ds.items() if k.startswith(("client-", "exit-"))]
        o = self.ctl_opts()
        script = " & ".join(
            f"(cat /tmp/repo.tgz | ssh {o} root@{ip} 'rm -rf {RS}/gnosis_vpn-testenv && mkdir -p {RS}/gnosis_vpn-testenv {RS}/bin && tar -xzf - -C {RS}/gnosis_vpn-testenv' && "
            # to a new name, then renamed over it: a localcluster still running there (a fleet that grew) makes the binary
            # busy and a plain scp onto it fails ("text file busy")
            f"scp -q {o} {LOCALCLUSTER} root@{ip}:{LOCALCLUSTER}.new && ssh {o} root@{ip} 'mv -f {LOCALCLUSTER}.new {LOCALCLUSTER}' && "
            f"ssh {o} root@{ip} 'sha256sum {LOCALCLUSTER}' | grep -q {self.state['sha']['localcluster']} && echo ok {ip} || echo FAIL {ip})"
            for ip in others) + " & wait"
        res = self.ssh(self.ctl, script, timeout=900)
        if "FAIL" in res or res.count("ok ") != len(others):
            raise RuntimeError(f"distributing the tree: {res}")
        log("control: specs of the fleet")
        with ThreadPoolExecutor(max_workers=16) as ex:       # each takes ~45 s (benchmarks, 10 s of vmstat): all at once
            list(ex.map(lambda d: self.specs(d["name"], d["public"], d), ds.values()))
        self.state["control_ready"] = True
        self.save()

    def write_hosts(self, tag, label):
        ds = self.droplets()
        r = self.state["relays"][label]

        def ms(group):
            ks = sorted((k for k in ds if k.startswith(group + "-") and k[len(group) + 1:].isdigit()), key=lambda k: int(k.rsplit("-", 1)[1]))
            return "\n".join(f'  {{ ssh = "root@{ds[k]["private"]}", addr = "{ds[k]["private"]}", name = "{k}", '
                             f'public = "{ds[k]["public"]}" }},' for k in ks)
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
        ips = [self.state["relays"][label]["private"]] + [d["private"] for k, d in ds.items() if k.startswith(("client-", "exit-"))]
        o = self.ctl_opts()
        pat = "'^vmstat -n -t 1'"
        if start:
            self.ssh(self.ctl, " ; ".join(f"ssh {o} root@{ip} \"pkill -f {pat}; setsid nohup vmstat -n -t 1 > /root/vmstat-{label}.txt 2>&1 < /dev/null &\""
                                          for ip in ips) + f"; pkill -f {pat}; setsid nohup vmstat -n -t 1 > /root/vmstat-ctl-{label}.txt 2>&1 < /dev/null & true")
            return
        d = self.out / "vmstat" / label
        d.mkdir(parents=True, exist_ok=True)
        names = {dd["private"]: dd["name"] for dd in ds.values()}
        names[self.state["relays"][label]["private"]] = self.state["relays"][label]["name"]
        for ip in ips:
            txt = self.ssh(self.ctl, f"ssh {o} root@{ip} \"pkill -f {pat}; cat /root/vmstat-{label}.txt\"", check=False)
            (d / f"{names.get(ip, ip)}.txt").write_text(txt)
        (d / f"gvpn-{self.name}-control.txt").write_text(self.ssh(self.ctl, f"pkill -f {pat}; cat /root/vmstat-ctl-{label}.txt", check=False))

    def fleet_up(self, label, when):
        """Every machine of the fleet running, else a RuntimeError naming the stopped ones: on 2026-10-08 six clients and
        exits of awsl1 were stopped through the EC2 API mid-ladder (by nothing in this repo), the rungs after it lost
        clients and connects, and the run went on as if the relay had failed."""
        down = {k: d for k, d in self.droplets().items() if d["status"] != "running"}
        if down:
            why = {k: (d.get("raw") or {}).get("StateTransitionReason", "") for k, d in down.items()}
            raise RuntimeError(f"relay {label}: fleet machine(s) not running {when}: "
                               + "; ".join(f"{k} {d['status']} {why[k]}".strip() for k, d in sorted(down.items())))

    def smart_rungs(self, size):
        """--smart: the clients per rung for a relay of this size, from its vCPUs. The predicted ceiling (--mbit-1core for
        one vCPU, else --mbit-per-core x vCPUs; awsl1, 2026-10-08: 6.2, then 8.2 / 16.7 / 33.5 at 2 / 4 / 8 cores) over
        what one client carries (--client-mbit) is the predicted saturation s; the rungs run to 2s+2 (capped at the
        topology), every one up to 8, else in steps of s/6, and T36's SATURATE_PCT ends them one rung past the measured
        saturation."""
        a = self.a
        vcpus = int(machineclass.lookup(machineclass.load(a.classes), f"{self.prov.name}:{size}")[2]["vcpus"])
        ceiling = a.mbit_1core if vcpus == 1 else a.mbit_per_core * vcpus
        s = max(1, math.ceil(ceiling / a.client_mbit))
        # rungs only over the clients doctor found healthy (failing ones are last in the topology)
        top = min(a.topology or a.clients, self.state.get("healthy_n") or 10 ** 6, 2 * s + 2)
        step = 1 if top <= 8 else max(1, math.ceil(s / 6))
        return sorted({1, *range(step, top + 1, step), top})

    def bring_up(self, label, n, first, tries=2, tag=""):
        """`multihost-up` of the shared n topology (a fresh chain unless one is running), tried `tries` times: a stack
        that fails part-way is taken down and brought up again whole."""
        for attempt in range(1, tries + 1):
            log(f"relay {label}: bringing the shared {n} topology up ({'fresh chain' if first else 'kept chain, adopted relay'})"
                + (f", attempt {attempt}" if attempt > 1 else ""))
            t0 = time.time()
            name = f"up-{label}{tag}" + (f"-{attempt}" if attempt > 1 else "")
            # control's CPU during the bring-up: it runs the chain (Anvil + Blokli) every node and client syncs from
            self.ssh(self.ctl, f"pkill -f '^vmstat -n -t 1$'; setsid nohup vmstat -n -t 1 > {RS}/logs/{name}.vmstat 2>&1 < /dev/null & true")
            env = "MULTIHOST_SETTLE_RESTART=0 MULTIHOST_REVIVE=0 " if self.a.no_repair else ""
            try:
                self.job(name, f"{env}just multihost-up {RS}/hosts-test-{label}.toml shared {n}"
                         + ("" if first else " --keep-chain") + " && sleep 60", timeout=3600)
            except RuntimeError as e:
                log(f"relay {label}: bring-up attempt {attempt} failed after {int(time.time() - t0)} s: {str(e)[-300:]}")
                if attempt == tries:
                    raise
                continue
            self.ssh(self.ctl, "pkill -f '^vmstat -n -t 1$'; true")
            self.timing(label, "up_s", time.time() - t0)
            self.state["chain"] = True
            self.save()
            self.chain_up.set()
            return

    def healthy(self, label, n):
        """`multihost-doctor --repair` before the test: every client connects and pings through its tunnel, a failing
        one restarted. If one still fails, the stack is brought up again (fresh chain) and checked once more; a client
        that fails on its own is the rig, and a rung lost to it says nothing about the relay (2026-10-08: 2-12 of 30
        clients per bring-up never found their 1-hop path)."""
        for attempt in ((1,) if self.a.no_repair else (1, 2)):
            t0 = time.time()
            try:
                self.job(f"doctor-{label}-{attempt}", f"just multihost-doctor {RS}/hosts-test-{label}.toml"
                         + ("" if self.a.no_repair else " --repair") + f" --allow-failing {self.a.allow_failing}", timeout=2400)
                self.timing(label, "doctor_s", time.time() - t0)
                doc = json.loads(self.ssh(self.ctl, "cat /tmp/gnosis_vpn-testenv/doctor.json"))
                with self.lock:
                    self.state["healthy_n"] = doc["healthy"]
                    self.state.setdefault("doctor", {})[label] = doc
                self.save()
                log(f"relay {label}: {doc['healthy']} of {n} clients connect and ping ({int(time.time() - t0)} s)"
                    + (f"; repaired: {', '.join(doc['repaired'])}" if doc["repaired"] else "")
                    + (f"; failing, moved last: {', '.join(doc['failing'])}" if doc["failing"] else ""))
                return
            except RuntimeError as e:
                log(f"relay {label}: doctor attempt {attempt}: {str(e)[-400:]}")
                if attempt == 2 or self.a.no_repair:
                    raise RuntimeError(f"relay {label}: clients failing doctor" + ("" if self.a.no_repair else
                                       " after a repair and a fresh bring-up")) from None
                self.chain_up.clear()
                self.bring_up(label, n, True, tag="-redo")

    def test(self, label, size, bring_up=True):
        first = not self.chain_up.is_set()
        self.write_hosts(f"test-{label}", label)
        if bring_up:
            n = self.a.topology or self.a.clients
            self.bring_up(label, n, first)
        self.healthy(label, n=self.a.topology or self.a.clients)
        knobs = dict(kv.split("=", 1) for kv in self.a.knob)
        knobs["T36_MACHINE"] = f"{self.prov.name}:{size}"
        if self.a.smart:
            knobs.setdefault("T36_LADDER", " ".join(map(str, self.smart_rungs(size))))
            knobs.setdefault("T36_SATURATE_PCT", str(self.a.saturate_pct))
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in knobs.items())
        run = f"{self.name}-{label}"
        log(f"relay {label}: {TEST}, run {run} ({env})")
        self.fleet_up(label, "before the test")
        self.vmstat(label, True)
        t0 = time.time()
        try:
            self.job(f"test-{label}", f"export {env}; SUITE_CELL={run} just multihost-test t36 --run-id {run}; echo pytest exit $?", timeout=6 * 3600)
        finally:
            self.vmstat(label, False)
            self.timing(label, "test_s", time.time() - t0)
        self.fetch(label, run)
        self.fleet_up(label, f"during the test (run {run} is void)")
        log(f"relay {label}: done, results in {self.out / 'runs' / run}")

    def timing(self, label, what, secs):
        with self.lock:
            self.state.setdefault("timing", {}).setdefault(label, {})[what] = round(secs)
        self.save()
        log(f"relay {label}: {what} = {round(secs)} s")

    # -- the in-place swap (AWS): one relay machine, resized between sizes ----------------------------------------
    def hot_swap(self, prev, label, size):
        """The relay machine of `prev` becomes `label`: its hoprd stopped (database and identity stay on the disk), the
        machine stopped, its type changed, started again (new hardware, same private address, so the address the node
        announced on-chain and every channel to it stay valid), its specs taken, the same hoprd started again. Exits
        and clients keep running: no `up`, no funding, no channel opened."""
        r = dict(self.state["relays"][prev])
        t0 = time.time()
        save = SAVE_HOPRD.replace("HOPRD", repr(HOPRD)).replace("DATA", repr(RELAY_DATA))
        n = self.ssh(r["public"], "python3 -", input=save).strip()
        if n in ("", "0"):
            raise RuntimeError(f"relay {prev}: no hoprd of {RELAY_DATA} running to carry over")
        self.ssh(r["public"], f"pkill -TERM -f '^{LOCALCLUSTER} .*{RELAY_DATA}'; pkill -TERM -f '^{HOPRD} '; "
                              f"for i in $(seq 1 60); do pgrep -f '^{HOPRD} ' >/dev/null || break; sleep 1; done; "
                              f"pkill -KILL -f '^{HOPRD} '; sync; true", timeout=120)
        log(f"relay {label}: {n} hoprd stopped on {r['name']} ({r['size']}); resizing to {size}")
        m = None
        for attempt in range(40):
            try:
                m = self.prov.resize(self.name, r["member"], size)
                break
            except SystemExit as e:
                if attempt == 39 or not any(w in str(e) for w in ("Capacity", "VcpuLimitExceeded", "InstanceLimitExceeded")):
                    raise RuntimeError(str(e))
                log(f"relay {label}: {str(e)[:160]}; retrying in 90 s")
                time.sleep(90)
        m, _ = providers.ready_wait(self.prov, self.name, r["member"], self.key, self.ready)
        if m["private"] != r["private"]:
            raise RuntimeError(f"relay {label}: private address changed {r['private']} -> {m['private']}")
        with self.lock:
            self.state["relays"][label] = dict(r, size=size, public=m["public"], ready=time.strftime("%FT%TZ", time.gmtime()),
                                               swapped_from=prev, started=None)
            self.state["relays"][prev]["handed_over"] = label
        self.save()
        self.timing(label, "resize_s", time.time() - t0)
        self.specs(f"{r['name']}@{size}", m["public"], m)
        relaunch = RELAUNCH_HOPRD.replace("DATA", repr(RELAY_DATA))
        out = self.ssh(m["public"], "python3 -", input=relaunch).strip()
        log(f"relay {label}: " + out.replace("\n", "; "))
        # the suite reads each node's pid from the stack's status (its CPU per process): the relaunched ones are new
        pids = dict(re.findall(r"apiPort (\d+) pid (\d+)", out))
        self.ssh(self.ctl, "python3 -", input=f"""
import json
p = "/tmp/gnosis_vpn-testenv/multihost.json"
st = json.load(open(p))
pids = {pids!r}
for n in st["nodes"]:
    port = n["api_url"].rsplit(":", 1)[-1].strip("/")
    if n["role"] == "relays" and port in pids:
        n["pid"] = int(pids[port])
json.dump(st, open(p, "w"), indent=2)
""")
        o = self.ctl_opts()
        urls = self.ssh(self.ctl, "python3 -c \"import json; print(' '.join(n['api_url'] for n in json.load(open('/tmp/gnosis_vpn-testenv/multihost.json'))['nodes'] "
                                  f"if n['role'] == 'relays'))\"").split()
        t1 = time.time()
        while not all(self.ssh(self.ctl, f"curl -s -o /dev/null -m 5 -w '%{{http_code}}' {u}/readyz", check=False).strip() == "200" for u in urls):
            if time.time() - t1 > 900:
                raise RuntimeError(f"relay {label}: {urls} not ready 900 s after the relaunch")
            time.sleep(5)
        self.timing(label, "relaunch_ready_s", time.time() - t1)
        with self.lock:
            self.state["relays"][label]["started"] = time.strftime("%FT%TZ", time.gmtime())
        self.save()
        time.sleep(self.a.settle)
        self.timing(label, "swap_total_s", time.time() - t0)

    def run_resize(self, todo):
        """--swap resize: one relay machine for the whole ladder; the first size starts the stack, every later size is
        a hot_swap of the same machine and node."""
        # after a fresh chain (a fleet that grew) the next size starts the stack again instead of a hot swap
        prev = self.state["done"][-1] if self.state["done"] and self.chain_up.is_set() else None
        if self.a.health_only:
            lab, size = todo[0]
            self.create_relay(lab, size, start=self.chain_up.is_set(), member="relay-hot")
            self.write_hosts(f"test-{lab}", lab)
            n = self.a.topology or self.a.clients
            if not (self.chain_up.is_set() and self.state.get("health_ok") == lab):
                self.bring_up(lab, n, not self.chain_up.is_set())
            self.healthy(lab, n)
            self.state["health_ok"] = lab
            self.save()
            log(f"health check passed: the stack is up on relay {lab} and every client connects and pings; rerun without "
                f"--health-only to run the ladder on it")
            return
        for lab, size in todo:
            try:
                if prev is None and self.state.get("health_ok") == lab and self.chain_up.is_set():
                    self.test(lab, size, bring_up=False)          # the stack --health-only brought up on this relay
                elif prev is None:
                    self.create_relay(lab, size, start=self.chain_up.is_set(), member="relay-hot")
                    self.test(lab, size, bring_up=True)
                else:
                    self.hot_swap(prev, lab, size)
                    self.test(lab, size, bring_up=False)
                self.state["done"].append(lab)
                prev = lab
                self.save()
            except Exception as e:      # noqa: BLE001
                log(f"relay {lab}: FAILED: {e}")
                with self.lock:
                    self.state["relays"].setdefault(lab, {})["test_error"] = str(e)[-2000:]
                self.save()
                break           # the relay machine's state after a failed swap is unknown: stop, read the log, rerun to resume
        log(f"ladder finished; the fleet is still up: `{Path(sys.argv[0]).name} destroy --provider {self.prov.name} --name {self.name}`")
        report(self.out)

    def fetch(self, label, run):
        d = self.out / "runs"
        d.mkdir(exist_ok=True)
        tgz = subprocess.run(do_fleet.ssh_argv(self.key, f"root@{self.ctl}") + [
            f"cd {RS}/runs && tar -czf - {run} -C {RS}/logs . -C {RS} hosts-test-{label}.toml "
            f"$(test -d /tmp/gnosis_vpn-testenv/client-logs && echo -C /tmp/gnosis_vpn-testenv client-logs)"], capture_output=True, timeout=600).stdout
        (d / f"{run}.tgz").write_bytes(tgz)
        subprocess.run(["tar", "-xzf", str(d / f"{run}.tgz"), "-C", str(d)], check=False)

    # -- the whole ladder ----------------------------------------------------------------------------------------
    def run(self):
        plan = self.plan()
        ds = self.base()
        self.setup_control(ds)
        todo = [(lab, size) for lab, size in plan if lab not in self.state["done"]]
        log(f"ladder {self.name}: {', '.join(lab for lab, _ in todo) or 'nothing left'}")
        if self.a.swap == "resize":
            return self.run_resize(todo)
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
    g.add_argument("--sizes", nargs="+", help="the provider's size names in order (a size twice: two machines)")
    r.add_argument("--provider", choices=("digitalocean", "aws"), default="digitalocean")
    r.add_argument("--snapshot", required=True, help="the base image of control, clients and exits: a DigitalOcean snapshot id "
                                                     "(do_fleet.py bake) or an AWS AMI id or name (aws_fleet.py bake)")
    r.add_argument("--relay-image", help="the relays' image (default: plain Ubuntu on DigitalOcean, the base image on AWS)")
    r.add_argument("--swap", choices=("new", "resize"), default="new",
                   help="new: a machine per relay size, every swap restarts exits and clients with fresh identities (8-9 min); "
                        "resize (AWS): one relay machine resized in place, its node and every channel kept, nothing else restarted")
    r.add_argument("--settle", type=int, default=60, help="seconds after a resized relay is ready before its test")
    r.add_argument("--localcluster", help="a hoprd-localcluster binary built with patches/hoprd-localcluster-max16.patch, replacing the snapshot's")
    r.add_argument("--client-image", help="default: the one gnosis_vpn-client image on the snapshot")
    r.add_argument("--server-image", help="default: the one gnosis_vpn-server image on the snapshot")
    r.add_argument("--clients", type=int, default=6, help="client machines and exit machines (the table's calibration: 6)")
    r.add_argument("--topology", type=int, help="clients and exit nodes in the stack, spread over those machines (default: --clients; "
                                                  "a smoke test on a small quota: 2 on one client and one exit machine)")
    r.add_argument("--base-size", default="c-8", help="clients, exits and control (the table's calibration: c-8)")
    r.add_argument("--control-size", help="control alone (AWS; default: --base-size): it runs the chain and the suite for every client")
    r.add_argument("--smart", action="store_true", help="per relay size, T36_LADDER from its vCPUs and T36_SATURATE_PCT: one rung past "
                                                       "saturation, not every rung (Ladder.smart_rungs)")
    r.add_argument("--mbit-per-core", type=float, default=4.2, help="--smart: predicted relay ceiling per vCPU from 2 vCPUs on, Mbit/s")
    r.add_argument("--mbit-1core", type=float, default=6.2, help="--smart: predicted ceiling of a 1-vCPU relay, Mbit/s")
    r.add_argument("--client-mbit", type=float, default=9.0, help="--smart: what one client carries on an idle relay, Mbit/s")
    r.add_argument("--saturate-pct", type=float, default=50, help="--smart: T36_SATURATE_PCT (suitelib/relaybench.py)")
    r.add_argument("--allow-failing", type=int, default=3, help="clients that may still fail doctor after a restart; they go last "
                                                              "and the rungs stop short of them")
    r.add_argument("--no-repair", action="store_true", help="no client restarts in the bring-up and no doctor repairs: see the stack as it "
                                                           "comes up (stability work)")
    r.add_argument("--health-only", action="store_true", help="--swap resize: fleet, first relay, bring-up and multihost-doctor, then "
                                                             "stop; a run without it then tests on that stack")
    r.add_argument("--region", default="lon1", help="where every size of the ladder is offered (the table's calibration: lon1)")
    r.add_argument("--prepare", type=int, default=3, help="relays made and started ahead of the one under test")
    r.add_argument("--knob", action="append", default=[], metavar="T36_VAR=VALUE", help="passed to the test (T36_MACHINE is set per relay)")
    rp = sub.add_parser("report", help="summary.md and summary.csv of the ladder's runs so far")
    cb = sub.add_parser("calibrate", help="rewrite the table's measurements and thresholds of the given classes from runs")
    cb.add_argument("runs", nargs="+", metavar="CLASS=RUNDIR")
    cb.add_argument("--source", help="what the calibration was measured on (the table's calibration.source)")
    cb.add_argument("--write", action="store_true", help="write the table (default: print it)")
    d = sub.add_parser("destroy", help="delete every machine of the fleet")
    d.add_argument("--provider", choices=("digitalocean", "aws"), default="digitalocean")
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
        providers.get(a.provider).destroy(a.name)
    elif a.cmd == "report":
        report(Path(a.out) / a.name)
    elif a.cmd == "calibrate":
        calibrate(a)
    else:
        Ladder(a).run()


if __name__ == "__main__":
    main()
