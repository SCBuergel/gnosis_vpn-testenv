"""The hoprd localcluster: status JSON, node REST and /metrics, node-to-node pings, live tc netem on the
host, and the 1 Hz node sampler."""
import json
import subprocess
import sys
import urllib.request

from pathlib import Path

from . import shell
from .verdicts import log, read_jsonl


class Cluster:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bin = cfg.localcluster_bin
        self.data_dir = cfg.data_dir
        self.size = cfg.cluster_size

    def available(self):
        """False on a production-network run (--no-cluster) or when the localcluster binary/status is not there."""
        if self.cfg.no_cluster:
            return False
        return Path(self.bin).exists() and self.status() is not None

    def status(self):
        raw = shell.out([self.bin, "status", "--data-dir", str(self.data_dir)], timeout=60)
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def state(self):
        st = self.status() or {}
        return st.get("state", "")

    def node(self, i):
        st = self.status() or {}
        try:
            return st["nodes"][i]
        except (KeyError, IndexError):
            return {}

    def field(self, i, key):
        v = self.node(i).get(key)
        return "" if v is None else v

    def api_url(self, i):
        return self.field(i, "api_url")

    def address(self, i):
        return self.field(i, "address")

    def pid(self, i):
        return int(self.field(i, "pid") or 0)

    def p2p_port(self, i):
        return (self.field(i, "p2p") or ":").rsplit(":", 1)[1]

    def log_file(self, i):
        return self.data_dir / "logs" / f"hoprd_{i}.log"

    def api(self, i, method, path, body=None, timeout=60):
        """One REST call to node i; returns the response text ('' on any error)."""
        hdr = {}
        tok = self.field(i, "api_token")
        if tok:
            hdr["x-auth-token"] = tok
        data = None
        if body is not None:
            data = json.dumps(body).encode() if not isinstance(body, (bytes, str)) else (body.encode() if isinstance(body, str) else body)
            hdr["content-type"] = "application/json"
        req = urllib.request.Request(self.api_url(i) + path, data=data, method=method, headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(errors="replace")
        except Exception as e:   # noqa: BLE001 - any failure is "no answer"
            body = getattr(e, "read", lambda: b"")()
            return body.decode(errors="replace") if body else ""

    def api_json(self, i, method, path, body=None, timeout=60, default=None):
        try:
            return json.loads(self.api(i, method, path, body, timeout))
        except ValueError:
            return default

    def open_outgoing(self, i):
        d = self.api_json(i, "GET", "/api/v4/channels", default={}) or {}
        return [c for c in d.get("outgoing", []) if c.get("status") == "Open"]

    def metrics(self, i):
        return self.api(i, "GET", "/metrics")

    def metric(self, i, name):
        for line in self.metrics(i).splitlines():
            p = line.split()
            if len(p) >= 2 and p[0] == name:
                try:
                    return float(p[1])
                except ValueError:
                    return None
        return None

    def ping(self, i, j):
        """Latency (ms) of a hoprd ping from node i to node j, or None."""
        d = self.api_json(i, "POST", f"/api/v4/peers/{self.address(j)}/ping", default={}) or {}
        return d.get("latency")

    # -- inter-node latency, live, no cluster restart ---------------------------------------------------------
    # On the single-host localcluster every node's P2P address is the Docker gateway, which routes via lo, so
    # tc netem on lo with a per-destination-port filter shapes one relay's traffic live. Needs root.
    NETEM_BANDS = 16          # the prio qdisc's bands; bands 1-2 carry the unimpaired traffic, 3.. one node each
    NETEM_MAX_NODES = NETEM_BANDS - 2

    def netem_apply(self, delays):
        """delays: {node_index: one_way_ms}; empty clears. At most NETEM_MAX_NODES (14) nodes can be impaired at once;
        no run the suite makes gets near it (CLUSTER_SIZE defaults to 3 and T09-impairment-ladder impairs at most
        two relays), so the guard is for a hand-built cluster, and it says so rather than reporting a tc return code."""
        iface = self.cfg.netem_iface
        if len(delays) > self.NETEM_MAX_NODES:
            log(f"netem: {len(delays)} nodes exceed the {self.NETEM_MAX_NODES} prio bands available; impairment NOT applied")
            return False
        shell.run(["tc", "qdisc", "del", "dev", iface, "root"], timeout=30)
        if not delays:
            return True
        if not shell.ok(["tc", "qdisc", "add", "dev", iface, "root", "handle", "1:", "prio", "bands", str(self.NETEM_BANDS)], timeout=30):
            return False
        band = 3
        for idx, ms in delays.items():
            port = self.p2p_port(idx)
            if not port:
                log(f"netem: no p2p port for node {idx}; impairment NOT applied")
                self.netem_clear()
                return False
            # every step checked: a missing sch_netem module or a rejected filter would otherwise leave the ladder
            # measuring an unimpaired path under an impaired label
            for cmd in (["tc", "qdisc", "add", "dev", iface, "parent", f"1:{band}", "handle", f"{band}0:", "netem",
                         "delay", f"{ms}ms", "limit", "20000"],
                        ["tc", "filter", "add", "dev", iface, "protocol", "ip", "parent", "1:0", "prio", "1", "u32",
                         "match", "ip", "dport", str(port), "0xffff", "flowid", f"1:{band}"]):
                r = shell.run(cmd, timeout=30)
                if r.returncode != 0:
                    log(f"netem: node {idx} (:{port}) +{ms}ms NOT applied: {' '.join(cmd[:4])}... rc={r.returncode} {r.stderr.strip()[:160]}")
                    self.netem_clear()
                    return False
            log(f"netem: node {idx} (:{port}) +{ms}ms")
            band += 1
        return True

    def netem_clear(self):
        shell.run(["tc", "qdisc", "del", "dev", self.cfg.netem_iface, "root"], timeout=30)

    @staticmethod
    def netem_status():
        """(netem qdiscs on the host, "") or (None, reason) when tc could not answer (missing, no permission): the
        caller must not read a failed check as "none", which is what shell.out's "" on a non-zero exit used to give.
        One tc call: the reason travels with the None."""
        r = shell.run("tc qdisc show", timeout=30)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip().splitlines()
            return None, (err[0][:120] if err else f"tc exited {r.returncode}")
        return r.stdout.count("netem"), ""

    @staticmethod
    def netem_count():
        return Cluster.netem_status()[0]

    # -- sampler ---------------------------------------------------------------------------------------------
    def sampler(self, run, name, interval=1, containers=()):
        """Context manager: node metrics + per-pid CPU + container CPU at `interval` s to samples/NAME.jsonl.
        Without a localcluster it samples nothing and rows() is empty."""
        if not self.available():
            return NullSampler(run, name)
        return NodeSampler(self, run, name, interval, containers)


class NullSampler:
    def __init__(self, run, name):
        self.path = run / "samples" / f"{name}.jsonl"

    def start(self):
        return self

    def stop(self):
        pass

    def rows(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NodeSampler:
    def __init__(self, cluster, run, name, interval, containers):
        self.cluster, self.run, self.name, self.interval, self.containers = cluster, run, name, interval, containers
        self.proc = None
        self.path = run / "samples" / f"{name}.jsonl"

    def start(self):
        c = self.cluster
        pids = ",".join(str(c.pid(i) or "") for i in range(c.size))   # the sampler's sentinel for "no pid" is the empty slot, not 0
        urls = ",".join(c.api_url(i) for i in range(c.size))
        script = Path(__file__).resolve().parent.parent / "node-sampler.py"
        self.proc = subprocess.Popen([sys.executable, str(script), "--pids", pids, "--urls", urls, "--interval",
                                      str(self.interval), "--containers", ",".join(self.containers), "--out", str(self.path)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def rows(self):
        # the sampler is killed at the end of the measurement; its last line may be cut short
        return read_jsonl(self.path, "sampler rows", missing_ok=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
