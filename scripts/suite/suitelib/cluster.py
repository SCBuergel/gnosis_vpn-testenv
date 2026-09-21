"""The hoprd localcluster: status JSON, node REST and /metrics, node-to-node pings, live tc netem on the
host, and the 1 Hz node sampler."""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import shell
from .verdicts import log


class Cluster:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bin = cfg.localcluster_bin
        self.data_dir = cfg.data_dir
        self.size = cfg.cluster_size

    def available(self):
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
    def netem_apply(self, delays):
        """delays: {node_index: one_way_ms}; empty clears."""
        iface = self.cfg.netem_iface
        shell.run(["tc", "qdisc", "del", "dev", iface, "root"], timeout=30)
        if not delays:
            return True
        if not shell.ok(["tc", "qdisc", "add", "dev", iface, "root", "handle", "1:", "prio", "bands", "16"], timeout=30):
            return False
        band = 3
        for idx, ms in delays.items():
            port = self.p2p_port(idx)
            if not port:
                log(f"netem: no p2p port for node {idx}")
                continue
            shell.run(["tc", "qdisc", "add", "dev", iface, "parent", f"1:{band}", "handle", f"{band}0:", "netem",
                       "delay", f"{ms}ms", "limit", "20000"], timeout=30)
            shell.run(["tc", "filter", "add", "dev", iface, "protocol", "ip", "parent", "1:0", "prio", "1", "u32",
                       "match", "ip", "dport", str(port), "0xffff", "flowid", f"1:{band}"], timeout=30)
            log(f"netem: node {idx} (:{port}) +{ms}ms")
            band += 1
        return True

    def netem_clear(self):
        shell.run(["tc", "qdisc", "del", "dev", self.cfg.netem_iface, "root"], timeout=30)

    @staticmethod
    def netem_count():
        return shell.out("tc qdisc show 2>/dev/null", timeout=30).count("netem")

    # -- sampler ---------------------------------------------------------------------------------------------
    def sampler(self, run, name, interval=1, containers=()):
        """Context manager: node metrics + per-pid CPU + container CPU at `interval` s to samples/NAME.jsonl."""
        return NodeSampler(self, run, name, interval, containers)


class NodeSampler:
    def __init__(self, cluster, run, name, interval, containers):
        self.cluster, self.run, self.name, self.interval, self.containers = cluster, run, name, interval, containers
        self.proc = None
        self.path = run / "samples" / f"{name}.jsonl"

    def start(self):
        c = self.cluster
        pids = ",".join(str(c.pid(i)) for i in range(c.size))
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
        try:
            return [json.loads(l) for l in open(self.path) if l.strip()]
        except FileNotFoundError:
            return []

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
