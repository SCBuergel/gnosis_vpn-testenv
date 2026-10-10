#!/usr/bin/env python3
"""T37-relay-outage on AWS: one client, N relays, one exit, each on a machine of its own, and the relays taken out
one a minute under a running download (tests/regression/test_t37_relay_outage.py).

    multihost/aws_python multihost/relay_outage.py run --name ro1 --snapshot gvpn-4.1.3-0.96.4-2026-10-08 \
        --localcluster ~/gnosis_vpn-testenv-logs/bin/hoprd-localcluster-4.1.3-max16-funder-preannounce
    multihost/aws_python multihost/relay_outage.py destroy --name ro1

The fleet: control (chain and suite, --control-size), client-1 and exit-1 (--base-size) and relay-1..N (--relay-size),
all from the baked AMI (aws_fleet.py bake). `run` creates it, brings the `multi-relay N` topology up
(`multihost-up HOSTS multi-relay N`), checks the client with `multihost-doctor`, runs the test, fetches the run to
--out/<name>/ and destroys the fleet (--keep: leaves it running). Machines, binaries and jobs on control as in
machine_ladder.py, whose Ladder this reuses."""
import argparse
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import aws_fleet  # noqa: E402
import do_fleet  # noqa: E402
from machine_ladder import HOPRD, LOCALCLUSTER, RS, Ladder, log  # noqa: E402

# tests/multihost.py's chain (start_chain), started here ahead of `up --chain-started`; not imported: it needs pytest
CHAIN_NAME, CHAIN_PORT, FUNDER_ACCOUNTS = "hopr-chain", 8080, 64
CHAIN_IMAGE = "europe-west3-docker.pkg.dev/hoprassociation/docker-images/bloklid-anvil:latest"


class Outage(Ladder):
    def specs(self, full, pub, d):
        """Machine specs and benchmarks (~60 s per machine) only with --specs: this test does not compare machines."""
        if self.a.specs:
            super().specs(full, pub, d)

    def relays(self, ds):
        return sorted((k for k in ds if k.startswith("relay-")), key=lambda k: int(k.rsplit("-", 1)[1]))

    def create(self):
        a = self.a
        if "control" not in self.droplets():
            log(f"creating fleet {self.name}: control {a.control_size}, client-1 and exit-1 {a.base_size}, {a.relays} x relay {a.relay_size}")
            aws_fleet.create(argparse.Namespace(name=self.name, clients=1, relays=0, exits=1, size=a.base_size, image=a.snapshot,
                                                pubkey=str(self.key) + ".pub", add=[f"relay:{a.relay_size}:{a.relays}"],
                                                control_size=a.control_size))
        # control first: the chain starts on it (~70 s to a Blokli that answers) while the other machines boot
        self.prov.wait(self.name, str(self.key), timeout=1500, only=["control"])
        ds = self.droplets()
        self.ctl, self.ctl_priv = ds["control"]["public"], ds["control"]["private"]
        if not self.state.get("up"):
            self.ssh(self.ctl, f"docker rm -f {CHAIN_NAME} >/dev/null 2>&1; docker run -d --rm --name {CHAIN_NAME} "
                               f"--platform linux/amd64 -e ANVIL_ACCOUNTS={FUNDER_ACCOUNTS} "
                               f"-p {self.ctl_priv}:{CHAIN_PORT}:{CHAIN_PORT} {CHAIN_IMAGE}", timeout=300)
            log("chain container started on control")
        self.prov.wait(self.name, str(self.key), timeout=1500)
        return self.droplets()

    def relay_binaries(self, ds):
        """The control's hoprd and localcluster on every relay (checked by sha256): the AMI's localcluster is older."""
        o, want = self.ctl_opts(), self.state["sha"]
        script = " & ".join(
            f"(ssh {o} root@{ds[k]['private']} 'mkdir -p {RS}/bin' && scp -q {o} {HOPRD} root@{ds[k]['private']}:{HOPRD}.new && "
            f"scp -q {o} {LOCALCLUSTER} root@{ds[k]['private']}:{LOCALCLUSTER}.new && "
            f"ssh {o} root@{ds[k]['private']} 'mv -f {HOPRD}.new {HOPRD} && mv -f {LOCALCLUSTER}.new {LOCALCLUSTER} && "
            f"sha256sum {HOPRD} {LOCALCLUSTER}' | tr '\\n' ' ' | sed 's/^/{k} /'; echo)" for k in self.relays(ds)) + " & wait"
        res = self.ssh(self.ctl, script, timeout=900)
        bad = [ln for ln in res.splitlines() if ln.strip() and not all(v in ln for v in want.values())]
        if bad or res.count(want["localcluster"]) != len(self.relays(ds)):
            raise RuntimeError(f"relay binaries: {res}")
        log(f"relays: hoprd and localcluster in place on {len(self.relays(ds))} machine(s)")

    def custom_client(self, ds):
        """--client-image-tar: a `docker save | gzip` of a client image (e.g. built on the build server), loaded on every
        client machine and made the stack's CLIENT_IMAGE (control's env.sh); takes effect at the next `up` (--reup)."""
        a = self.a
        if not a.client_image_tar:
            return
        tag = a.client_image
        if not tag:
            raise SystemExit("--client-image-tar needs --client-image (the tag inside the tarball)")
        o = self.ctl_opts()
        self.ssh(self.ctl, "cat > /tmp/client-image.tar.gz", input=Path(a.client_image_tar).expanduser().read_bytes(), timeout=900)
        clients = [k for k in ds if k.startswith("client-")]
        res = self.ssh(self.ctl, " & ".join(f"(scp -q {o} /tmp/client-image.tar.gz root@{ds[k]['private']}:/tmp/ && "
                                            f"ssh {o} root@{ds[k]['private']} 'gunzip -c /tmp/client-image.tar.gz | docker load' | tail -1)"
                                            for k in clients) + " & wait", timeout=900)
        if res.count(tag) != len(clients):
            raise RuntimeError(f"loading {tag} on the clients: {res}")
        self.ssh(self.ctl, f"sed -i 's|^export CLIENT_IMAGE=.*SERVER_IMAGE=|export CLIENT_IMAGE={tag} SERVER_IMAGE=|' {RS}/env.sh && grep CLIENT_IMAGE {RS}/env.sh")
        self.state["client_image"] = tag
        self.save()
        log(f"client image {tag} loaded on {len(clients)} client machine(s)")

    def custom_exit_hoprd(self, ds):
        """--exit-hoprd: a hoprd binary (e.g. built on the build server) put on every exit machine at the stack's hoprd
        path, via control; the relays keep the stock one. Takes effect at the next `up` (--reup)."""
        a = self.a
        if not a.exit_hoprd:
            return
        o = self.ctl_opts()
        self.ssh(self.ctl, "cat > /tmp/exit-hoprd && chmod +x /tmp/exit-hoprd", input=Path(a.exit_hoprd).expanduser().read_bytes(), timeout=900)
        want = self.ssh(self.ctl, "sha256sum /tmp/exit-hoprd").split()[0]
        exits = [k for k in ds if k.startswith("exit-")]
        res = self.ssh(self.ctl, " & ".join(
            f"(scp -q {o} /tmp/exit-hoprd root@{ds[k]['private']}:{HOPRD}.new && "
            f"ssh {o} root@{ds[k]['private']} 'mv -f {HOPRD}.new {HOPRD} && sha256sum {HOPRD}')" for k in exits) + " & wait", timeout=900)
        if res.count(want) != len(exits):
            raise RuntimeError(f"exit hoprd: {res}")
        self.state["exit_hoprd"] = want
        self.save()
        log(f"exit hoprd {want[:12]} on {len(exits)} exit machine(s)")

    def write_outage_hosts(self, ds):
        def ms(keys):
            return "\n".join(f'  {{ ssh = "root@{ds[k]["private"]}", addr = "{ds[k]["private"]}", name = "{k}", public = "{ds[k]["public"]}" }},'
                             for k in keys)
        toml = f"""# relay outage {self.name}: {len(self.relays(ds))} relays, one exit, one client
[ssh]
options = "{self.ctl_opts()}"

[roles.chain]
ssh = "local"
addr = "{self.ctl_priv}"

[roles.relays]
hoprd_bin = "{HOPRD}"
localcluster_bin = "{LOCALCLUSTER}"
machines = [
{ms(self.relays(ds))}
]

[roles.exits]
hoprd_bin = "{HOPRD}"
localcluster_bin = "{LOCALCLUSTER}"
repo = "{RS}/gnosis_vpn-testenv"
server_image = "{self.state['server_image']}"
machines = [
{ms(['exit-1'])}
]

[roles.clients]
repo = "{RS}/gnosis_vpn-testenv"
machines = [
{ms(['client-1'])}
]
"""
        self.ssh(self.ctl, f"cat > {RS}/hosts-outage.toml", input=toml)

    def log_env(self):
        """The stack's log levels (--node-log for every hoprd, --client-log for the client) as env assignments."""
        env = {}
        if self.a.node_log:
            env["MULTIHOST_NODE_LOG"] = self.a.node_log
        if self.a.node_env:
            env["MULTIHOST_NODE_ENV"] = self.a.node_env
        if self.a.client_log:
            env["CLIENT_LOG_LEVEL"] = self.a.client_log
        return " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())

    def node_logs(self, run):
        """Every relay's and the exit's hoprd logs, and the client container's whole log, into the run's nodes/."""
        ds = self.droplets()
        o = self.ctl_opts()
        dest = f"{RS}/runs/{run}/nodes"
        parts = [f"mkdir -p {dest}"]
        for k in self.relays(ds) + ["exit-1"]:
            data = "/tmp/hopr-mh-relays" if k.startswith("relay-") else "/tmp/hopr-mh-exits"
            parts.append(f"(ssh {o} root@{ds[k]['private']} 'tar -czf - -C {data} logs 2>/dev/null' > {dest}/{k}.tgz)")
        parts.append(f"(ssh {o} root@{ds['client-1']['private']} 'docker logs gnosis_vpn-client 2>&1 | gzip' > {dest}/client-1.log.gz)")
        self.ssh(self.ctl, " ; ".join(parts[:1]) + " ; " + " & ".join(parts[1:]) + " & wait", timeout=900, check=False)

    def outage_fetch(self, run):
        d = self.out / "runs"
        d.mkdir(exist_ok=True)
        tgz = subprocess.run(do_fleet.ssh_argv(self.key, f"root@{self.ctl}") + [
            f"cd {RS}/runs && tar -czf - {run} -C {RS}/logs . -C {RS} hosts-outage.toml "
            f"$(test -d /tmp/gnosis_vpn-testenv/client-logs && echo -C /tmp/gnosis_vpn-testenv client-logs)"],
            capture_output=True, timeout=600).stdout
        (d / f"{run}.tgz").write_bytes(tgz)
        subprocess.run(["tar", "-xzf", str(d / f"{run}.tgz"), "-C", str(d)], check=False)
        log(f"results in {d / run}")

    def go(self):
        a = self.a
        t0 = time.time()
        if a.reup:
            # a fresh chain too (create() restarts it): `up --chain-started` wants one with nothing on it
            self.state["up"] = False
        ds = self.create()
        self.setup_control(ds)
        self.relay_binaries(ds)
        self.custom_client(ds)
        self.custom_exit_hoprd(ds)
        self.write_outage_hosts(ds)
        if not self.state.get("up"):
            # no client restart while its channels open: the client opens them one at a time (~25 s each), and the
            # revival made for one-channel clients (180 s) restarted it mid-way with 10 relays (ro2, 2026-10-09)
            log(f"bringing the multi-relay {a.relays} topology up")
            self.job("up-outage", f"{self.log_env()} MULTIHOST_REVIVE=0 just multihost-up {RS}/hosts-outage.toml multi-relay {a.relays} --chain-started --timeout 1800", timeout=3600)
            self.state["up"] = True
            self.save()
        log(f"stack up after {int(time.time() - t0)} s; doctor")
        self.job("doctor-outage", f"just multihost-doctor {RS}/hosts-outage.toml", timeout=1200)
        knobs = dict(kv.split("=", 1) for kv in a.knob)
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in knobs.items())
        n = 1 + sum(1 for r in self.state.get("runs", []))
        run = f"{self.name}-t37-{n}"
        log(f"T37-relay-outage, run {run}" + (f" ({env})" if env else ""))
        try:
            self.job(f"test-{run}", (f"export {env}; " if env else "") + ("export SAVE_LOG_RAW=1; " if a.node_logs else "") + f"SUITE_CELL={run} just multihost-test t37 --run-id {run}; "
                     "echo pytest exit $?", timeout=3 * 3600)
        finally:
            self.state.setdefault("runs", []).append(run)
            self.save()
            if a.node_logs:
                self.node_logs(run)
            self.outage_fetch(run)
        if not a.keep:
            self.prov.destroy(self.name)
            log(f"fleet {self.name} destroyed")
        log(f"done after {int(time.time() - t0)} s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--snapshot", required=True, help="the baked AMI (name or id)")
    r.add_argument("--relays", type=int, default=5)
    r.add_argument("--relay-size", default="c7a.xlarge")
    r.add_argument("--base-size", default="c7a.large", help="client-1 and exit-1")
    r.add_argument("--control-size", default="c7a.xlarge")
    r.add_argument("--localcluster", help="a localcluster with the max16, funder-key and preannounce patches, replacing the AMI's")
    r.add_argument("--client-image", help="the client image's tag (with --client-image-tar: the one inside it)")
    r.add_argument("--exit-hoprd", help="a local hoprd binary for the exit machines (the relays keep the stock one)")
    r.add_argument("--client-image-tar", help="a local `docker save | gzip` of a custom client image to load on the clients")
    r.add_argument("--server-image")
    r.add_argument("--knob", action="append", default=[], metavar="T37_VAR=VALUE")
    r.add_argument("--keep", action="store_true", help="leave the fleet running after the test")
    r.add_argument("--specs", action="store_true", help="record every machine's specs and benchmarks (~60 s)")
    r.add_argument("--node-log", help="RUST_LOG of every hoprd (relays, exit), e.g. 'info,hopr_transport_session=debug'")
    r.add_argument("--node-env", help="more 'K=V K=V' for every hoprd")
    r.add_argument("--client-log", help="RUST_LOG of the client (default: the justfile's CLIENT_LOG_LEVEL)")
    r.add_argument("--reup", action="store_true", help="a fresh chain and stack on a kept fleet (new log levels or binaries)")
    r.add_argument("--node-logs", action="store_true", help="fetch every node's and the client's whole log after the test (keeps the client's DEBUG lines)")
    d = sub.add_parser("destroy")
    for p in (r, d):
        p.add_argument("--name", required=True)
        p.add_argument("--key", default="~/.ssh/do_testenv_fleet_ed25519")
        p.add_argument("--out", default="~/gnosis_vpn-testenv-logs/relay-outage")
    a = ap.parse_args()
    # what Ladder reads and this run does not vary
    for k, v in dict(provider="aws", classes=None, relay_image=None, swap="resize", project=None, region="eu-north-1",
                     clients=1, no_repair=True, allow_failing=0, sizes=None, ladder=None, smart=False, topology=None).items():
        setattr(a, k, getattr(a, k, v))
    o = Outage(a)
    if a.cmd == "destroy":
        o.prov.destroy(a.name)
        return
    o.go()


if __name__ == "__main__":
    main()
