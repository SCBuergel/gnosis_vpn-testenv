"""The gnosis_vpn client under test: control through `docker exec <container> gnosis_vpn-ctl`, connect and
disconnect with an armed deadman, client-log error counters, telemetry, log slices, in-container helpers."""
import atexit
import json
import os
import re
import signal
import subprocess
import time

from . import shell
from .verdicts import log, utc_now


class ConnectFailed(Exception):
    pass


_all_clients = []


def _disarm_all():
    for c in _all_clients:
        c.disarm_deadman()


atexit.register(_disarm_all)


# Only WARN/ERROR lines count for the error classes; DEBUG chatter (routing_actor "should_reconnect", telemetry
# dumps that mention "reassembly" in metric help text) produced false positives. The periodic liveness ping only
# logs its result at DEBUG; it is counted regardless of level because three of them are what a "reconnect" is
# made of, and a reconnect with N ping timeouts before it is a liveness-target problem (T01) until proven otherwise.
_LEVEL = re.compile(r"\b(WARN|ERROR)\b")
_PINGFAIL = re.compile(r"TunnelPingResult: Error\(Ping timed out\)")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ERROR_PATTERNS = {
    "frame_discarded": re.compile(r"frame.*(discard|expired)|discard.*frame", re.I),
    "reassembly_failed": re.compile(r"failed to reassemble", re.I),
    "decap_error": re.compile(r"decapsulat", re.I),
    "tunnel_ping_exceeded": re.compile(r"tunnel ping exceeded", re.I),
    "decap_stalled": re.compile(r"DecapStalled"),
    "reconnects": re.compile(r"exceeded max failures - reconnecting|restarting connection worker|requesting reconnect", re.I),
    "ping_timeouts": _PINGFAIL,
    "no_surb": re.compile(r"no surb|NoSurb|out of surb", re.I),
    "warn_error_lines": re.compile(r"."),
}
# the DEBUG path-planner/selector lines (~40 MB/min, about 90 % of the volume) are dropped from saved logs
_PLANNER_DEBUG = re.compile(r"DEBUG.*hopr_transport::path::(planner|selector)")


def count_log_errors(lines):
    c = {k: 0 for k in _ERROR_PATTERNS}
    for line in lines:
        line = _ANSI.sub("", line)
        if "received worker response" in line:
            continue
        if _PINGFAIL.search(line):
            c["ping_timeouts"] += 1
            continue
        if not _LEVEL.search(line):
            continue
        for k, p in _ERROR_PATTERNS.items():
            if p.search(line):
                c[k] += 1
    return c


def telemetry_sum(text, name):
    """Summed value of every series named `name` in a Prometheus text dump (labels ignored); None if absent."""
    tot, n = 0.0, 0
    for line in text.replace("\\n", "\n").splitlines():
        line = line.strip().strip('"')
        if line.startswith(name) and line[len(name):len(name) + 1] in ("{", " "):
            try:
                tot += float(line.rsplit(" ", 1)[1])
                n += 1
            except (ValueError, IndexError):
                pass
    if n == 0:
        return None
    return int(tot) if tot == int(tot) else tot


class Session:
    """A connected tunnel: connect_ms, since (a docker --since stamp taken before the connect), iface.
    Leaving the `with` block disconnects."""

    def __init__(self, client, dest, connect_ms, since, iface):
        self.client, self.dest, self.connect_ms, self.since, self.iface = client, dest, connect_ms, since, iface

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.client.disconnect()
        return False

    def errors(self):
        return self.client.log_errors(self.since)

    def save_log(self, name):
        return self.client.save_log(name, self.since)


class Client:
    """One client container. Run helpers against another client by making another Client(cfg, run, name)."""

    suite_dir = "/suite"   # scripts/suite is mounted read-only at /suite in the tools sidecar

    def __init__(self, cfg, run, name=None, index=1):
        self.cfg, self.run = cfg, run
        self.name = name or (cfg.client if index == 1 else f"{cfg.client}-{index}")
        self.tools = f"{self.name}-tools"    # the sidecar in this client's network namespace (just _tools-start)
        self._tools_checked = None
        self.index = index
        self.deadman_s = cfg.deadman
        self._deadman = None
        self.iface = None
        self.session = None
        _all_clients.append(self)

    def __repr__(self):
        return f"Client({self.name})"

    # -- containers ------------------------------------------------------------------------------------------
    def exists(self):
        return shell.ok(["docker", "container", "inspect", self.name], timeout=30)

    def tools_exists(self):
        return shell.ok(["docker", "container", "inspect", self.tools], timeout=30)

    def _shell_target(self):
        """Where shell commands run: the tools sidecar (curl, ping, ip, python; the client's network namespace).
        Without one, T01-topology-preconditions fails the run (the upstream client image has no curl or python3, and
        a run without the sidecar reads zero bytes with no error); the fallback to the client container itself is
        for --no-cluster runs against a client the suite did not start, and is said once."""
        if self._tools_checked is None:
            self._tools_checked = self.tools_exists()
            if not self._tools_checked:
                log(f"{self.name}: no tools sidecar {self.tools}; commands run in the client container itself "
                    f"(only meaningful with --no-cluster against a client with curl and python3)")
        return self.tools if self._tools_checked else self.name

    def exec(self, cmd, timeout=shell.DEFAULT_TIMEOUT):
        """Run a shell command in the client's network namespace (the tools sidecar)."""
        return shell.run(["docker", "exec", self._shell_target(), "sh", "-c", cmd], timeout=timeout)

    def exec_client(self, cmd, timeout=shell.DEFAULT_TIMEOUT):
        """Run a shell command inside the client container itself (its binaries, its process namespace)."""
        return shell.run(["docker", "exec", self.name, "sh", "-c", cmd], timeout=timeout)

    def out(self, cmd, timeout=shell.DEFAULT_TIMEOUT, default=""):
        r = self.exec(cmd, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else default

    def out_client(self, cmd, timeout=shell.DEFAULT_TIMEOUT, default=""):
        r = self.exec_client(cmd, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else default

    def exec_bg(self, cmd):
        """Start a shell command in the client's network namespace, detached."""
        return shell.run(["docker", "exec", "-d", self._shell_target(), "sh", "-c", cmd], timeout=60)

    def ctl(self, *args, timeout=60):
        r = shell.run(["docker", "exec", self.name, "gnosis_vpn-ctl", *args], timeout=timeout)
        return r.stdout if r.returncode == 0 else ""

    def ctl_json(self, *args, timeout=60):
        raw = shell.run(["docker", "exec", self.name, "gnosis_vpn-ctl", "-o", "json", *args], timeout=timeout).stdout
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def version(self):
        return self.ctl("--version").strip() or "unknown-client"

    # -- status ----------------------------------------------------------------------------------------------
    def status_text(self):
        return self.ctl("status")

    def is_connected(self):
        # 0.96.x prints "Connected to" on its own line, not the first
        return any(l.startswith("Connected to") for l in self.status_text().splitlines())

    def worker_online(self):
        first = (self.status_text().splitlines() or [""])[0]
        return "Worker offline" not in first

    def destinations(self):
        return re.findall(r"^(\S+) Route health:", self.status_text(), re.M)

    def dest_health_line(self, dest=None):
        d = dest or self.cfg.dest
        for l in self.status_text().splitlines():
            if l.startswith(f"{d} Route health:"):
                return l
        return ""

    def dest_is_ready(self, dest=None):
        return "Ready to connect" in self.dest_health_line(dest)

    def wait_worker(self, timeout=120):
        waited = 0
        while not self.worker_online():
            time.sleep(2)
            waited += 2
            if waited >= timeout:
                log(f"{self.name}: worker still offline after {timeout}s")
                return False
        return True

    def wait_dest_ready(self, dest=None, timeout=None):
        d = dest or self.cfg.dest
        t = timeout or self.cfg.connect_timeout
        waited = 0
        while not self.dest_is_ready(d):
            time.sleep(3)
            waited += 3
            if waited >= t:
                log(f"{self.name}: destination {d} not Ready after {t}s: {self.dest_health_line(d)}")
                return False
        return True

    def channels_out(self):
        """Number of outgoing channels the client's balance reports."""
        d = self.ctl_json("balance")
        try:
            b = d.get("Balance", d)
            b = b.get("Ok", b)
            return len(b.get("channels_out", []))
        except AttributeError:
            return 0

    # -- deadman ---------------------------------------------------------------------------------------------
    # The deadman disconnects the client after deadman_s seconds whatever happens to this process, so the kill
    # switch can never strand a host. It runs fully detached (own session, stdio closed). `sleep && disconnect`,
    # not `sleep; disconnect`: when disarm kills the sleep the shell must not fall through to the disconnect
    # (it did, and every connect made from a subshell disconnected its own client ~30 s later: fullrun5's
    # T22-concurrent-clients read zero bytes on five of seven clients). disarm kills the shell first.
    def deadman_cover(self, seconds):
        """Raise the deadman so it cannot fire inside a session that must last `seconds` (plus ramp and report).
        Call before connect(). T23 (3600 s) and T24 (900 s) once ran under the 900 s default: the deadman
        disconnected the client at +15 min and the probe counted the rest as loss."""
        self.deadman_s = max(self.deadman_s, int(seconds) + self.cfg.surb_ramp_wait + 120)

    def arm_deadman(self):
        self.disarm_deadman()
        cmd = f"sleep {self.deadman_s} && docker exec {self.name} gnosis_vpn-ctl disconnect >/dev/null 2>&1"
        self._deadman = shell.detached(cmd)
        (self.run / f".deadman-{self.name}.pid").write_text(str(self._deadman.pid))

    def disarm_deadman(self):
        p = self._deadman
        self._deadman = None
        if p is None:
            return
        kids = shell.out(["pgrep", "-P", str(p.pid)], timeout=10).split()
        try:
            os.kill(p.pid, signal.SIGTERM)      # the shell first, so it can never run the disconnect on its way out
        except ProcessLookupError:
            pass
        for k in kids:
            try:
                os.kill(int(k), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            (self.run / f".deadman-{self.name}.pid").unlink()
        except FileNotFoundError:
            pass

    # -- connect / disconnect --------------------------------------------------------------------------------
    # The idle after connect is floored at SURB_RAMP_WAIT (25 s) so a measurement clears the client's SURB ramp
    # (0.96.2: 20 s; older branches: 60 s). DO NOT raise it: a 75 s default was tried and manufactured failures.
    # During a long post-connect idle the return-path SURBs expire ("evicting surb ... cause=Expired"), the first
    # sustained transfer afterwards starves the return path, the tunnel ping times out and the watchdog
    # reconnects, killing whichever transfer ran first (8/8 failed at 75 s on two hoprd versions, 6/6 passed at
    # a short wait). If a client needs a longer ramp, shorten the ramp (GNOSISVPN_SURB_RAMP_SECS) instead.
    # Tests that measure the ramp itself (T07's cold arm, T15) pass ramp_wait_opt_out=True; nothing else should.
    def connect(self, dest=None, idle=0, ramp_wait_opt_out=False):
        d = dest or self.cfg.dest
        if not self.wait_dest_ready(d):
            raise ConnectFailed(f"{d} not Ready: {self.dest_health_line(d)[:120]}")
        since = utc_now()
        self.arm_deadman()
        t0 = time.time()
        self.ctl("connect", d)
        while not self.is_connected():
            time.sleep(1)
            if time.time() - t0 >= self.cfg.connect_timeout:
                head = (self.status_text().splitlines() or [""])[0]
                log(f"{self.name}: connect to {d} timed out after {self.cfg.connect_timeout}s: {head}")
                self.ctl("disconnect")
                self.disarm_deadman()
                raise ConnectFailed(f"connect to {d} timed out after {self.cfg.connect_timeout}s")
        connect_ms = int((time.time() - t0) * 1000)
        self.iface = self.out('ls /sys/class/net | grep -m1 "^wg" || true')
        if not ramp_wait_opt_out and idle < self.cfg.surb_ramp_wait:
            idle = self.cfg.surb_ramp_wait
        if idle > 0:
            time.sleep(idle)
        self.session = Session(self, d, connect_ms, since, self.iface)
        return self.session

    def disconnect(self):
        self.ctl("disconnect")
        for _ in range(60):
            if not self.is_connected():
                break
            time.sleep(1)
        self.disarm_deadman()
        self.session = None

    def restart(self, ready_timeout=None):
        """Stop/start the container (config or image changes); waits for the worker and for the primary
        destination to be Ready again (a fresh worker re-syncs and health-checks before it can connect)."""
        cwd = str(self.cfg.testenv_dir)
        if not (shell.ok("just client-stop", timeout=120, cwd=cwd) and shell.ok("just client-start", timeout=300, cwd=cwd)):
            log("client restart failed")
            return False
        time.sleep(3)
        if not self.wait_worker(180):
            return False
        t = ready_timeout or int(self.cfg.env.get("RESTART_READY_TIMEOUT", "600"))
        if not self.wait_dest_ready(self.cfg.dest, t):
            # keep the evidence: the container is recreated with --rm on the next restart and its log is gone with it
            ts = time.strftime("%H%M%S", time.gmtime())
            with open(self.run / "logs" / f"restart-not-ready-{ts}.log", "w") as f:
                f.write("== status\n" + self.status_text() + "\n== config\n")
                try:
                    f.write(open(self.cfg.config_dir / "client.toml").read())
                except OSError:
                    pass
                f.write("\n== log (last 12 min)\n" + shell.run(["docker", "logs", "--since", "12m", self.name], timeout=120).stdout)
            log(f"warning: {self.cfg.dest} not Ready {t}s after client restart (evidence: logs/restart-not-ready-{ts}.log)")
        return True

    # -- logs and telemetry ----------------------------------------------------------------------------------
    def log_lines(self, since):
        """Iterate the container log since a docker --since stamp (streamed: the debug log is large)."""
        p = subprocess.Popen(["timeout", "600", "docker", "logs", "--since", since, self.name], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace")
        try:
            for line in p.stdout:
                yield line
        finally:
            p.stdout.close()
            p.wait(timeout=60)

    def log_errors(self, since):
        return count_log_errors(self.log_lines(since))

    def count_log(self, since, pattern):
        rx = re.compile(pattern)
        return sum(1 for l in self.log_lines(since) if rx.search(l))

    def save_log(self, name, since):
        """The client's log slice for the window, without the DEBUG path-planner lines unless SAVE_LOG_RAW=1:
        fullrun5 saved 30 GB of them and fullrun6 died at T10 with the disk full."""
        path = self.run / "logs" / f"{name}.log"
        with open(path, "w") as f:
            for line in self.log_lines(since):
                if self.cfg.save_log_raw or not _PLANNER_DEBUG.search(line):
                    f.write(line)
        return path

    def telemetry_metric(self, name):
        d = self.ctl_json("telemetry")
        if d is None:
            return None
        txt = d if isinstance(d, str) else json.dumps(d)
        return telemetry_sum(txt, name)

    def worker_rss_kb(self):
        """The worker's RSS from /proc inside the client container (its pid namespace; busybox ps has no -C)."""
        v = self.out_client("p=$(pidof gnosis_vpn-worker | cut -d' ' -f1); [ -n \"$p\" ] && awk '/VmRSS/{print $2}' /proc/$p/status").strip()
        return int(v) if v.isdigit() else 0

    def log_path_size(self):
        p = shell.out(["docker", "inspect", "-f", "{{.LogPath}}", self.name], timeout=30)
        try:
            return os.stat(p).st_size
        except OSError:
            return 0

    # -- per-second rx/tx sampler on the tunnel interface, inside the container -----------------------------
    def persec_start(self, name):
        f = f"{self.run.in_client}/persec-{name}.csv"
        iface = self.iface
        self.exec(f"rm -f {f}; nohup sh -c 'while [ -d /sys/class/net/{iface} ]; do echo $(date +%s),$(cat /sys/class/net/{iface}/statistics/rx_bytes),$(cat /sys/class/net/{iface}/statistics/tx_bytes) >> {f}; sleep 1; done' >/dev/null 2>&1 & echo $! > /tmp/persec.pid")

    def persec_stop(self):
        self.exec("kill $(cat /tmp/persec.pid 2>/dev/null) 2>/dev/null; true")

    def persec_stall(self, name, direction):
        """Longest run of zero-progress seconds in a persec CSV (rx or tx)."""
        col = 1 if direction == "rx" else 2
        try:
            rows = [l.strip().split(",") for l in open(self.run / f"persec-{name}.csv") if l.strip()]
        except FileNotFoundError:
            return 0
        prev, run, best = None, 0, 0
        for r in rows:
            try:
                v = int(r[col])
            except (IndexError, ValueError):
                continue
            if prev is not None:
                run = run + 1 if v == prev else 0
                best = max(best, run)
            prev = v
        return best

    # -- probes (scripts/suite/probes, run in the tools sidecar) ---------------------------------------------
    def probe_cmd(self, probe, out, **args):
        parts = [f"python3 {self.suite_dir}/probes/{probe}.py"]
        for k, v in args.items():
            if v is True:
                parts.append(f"--{k.replace('_', '-')}")
            elif v is not None and v is not False:
                parts.append(f"--{k.replace('_', '-')} {v}")
        parts.append(f"--out {self.run.in_client}/{out}")
        return " ".join(parts)

    def probe(self, probe, out, timeout, **args):
        """Run a probe to completion in the sidecar; the result JSON lands in the run directory."""
        return self.exec(self.probe_cmd(probe, out, **args) + " >/dev/null 2>&1", timeout=timeout)

    def probe_bg(self, probe, out, **args):
        return self.exec_bg(self.probe_cmd(probe, out, **args))

    def kill_probes(self):
        """Stop any probe still running in the sidecar (after a test timeout the local docker exec dies, the
        probe does not); the pattern is anchored so it cannot match its own shell."""
        if self.exists():
            self.exec("pkill -f '^python3 /suite/probes/' 2>/dev/null; true", timeout=30)


def clients_running(cfg, run, limit=16):
    """Client objects for the consecutive client containers that are up, starting at the primary."""
    out = []
    for i in range(1, limit + 1):
        c = Client(cfg, run, index=i)
        if not c.exists():
            _all_clients.remove(c)
            break
        out.append(c)
    return out


def connect_or_fail(checks, client, dest=None, idle=0, label="", **kw):
    """connect(); on failure record a FAIL (or a RECORDED line for a diagnostic) and return None."""
    try:
        return client.connect(dest, idle, **kw)
    except ConnectFailed as e:
        prefix = f"{label}: " if label else ""
        checks.failed(f"{prefix}connect failed: {e}")
        return None
