"""Where a command runs: this machine, or another one over ssh (the multi-machine testenv, tests/multihost.py).

A Host is built from an ssh target ("root@10.114.0.16") or one of LOCAL for this machine. Every call has a timeout
(shell.run). cpu_sample() reads /proc on that machine, so CPU is measured where the process runs. Sampler streams
/proc from that machine for the length of a phase (CPU, per-process CPU, interface byte counters, UDP error counters),
each sample stamped with the machine's own clock, so a window can be cut out of it afterwards."""
import shlex
import subprocess
import threading
import time

from . import shell

LOCAL = (None, "", "local", "localhost")
SSH_BASE = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15"]


class Host:
    def __init__(self, ssh=None, ssh_opts=""):
        self.ssh = None if ssh in LOCAL else ssh
        self.opts = shlex.split(ssh_opts or "")

    @property
    def local(self):
        return self.ssh is None

    def __repr__(self):
        return f"Host({self.ssh or 'local'})"

    def argv(self, cmd):
        if self.local:
            return ["sh", "-c", cmd]
        return ["ssh", *SSH_BASE, *self.opts, self.ssh, cmd]

    def run(self, cmd, timeout=shell.DEFAULT_TIMEOUT, input=None):
        return shell.run(self.argv(cmd), timeout=timeout, input=input)

    def out(self, cmd, timeout=shell.DEFAULT_TIMEOUT, default=""):
        r = self.run(cmd, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else default

    def ok(self, cmd, timeout=shell.DEFAULT_TIMEOUT):
        return self.run(cmd, timeout=timeout).returncode == 0

    def cpu_sample(self, pids=()):
        """(busy jiffies, total jiffies over all cores, {pid: utime+stime seconds}) on this machine; one call. A pid that
        is gone reads None."""
        pids = [int(p) for p in pids if p]
        cmd = "getconf CLK_TCK; head -1 /proc/stat" + "".join(
            f"; printf '%s ' {p}; cat /proc/{p}/stat 2>/dev/null || echo" for p in pids)
        lines = self.out(cmd, timeout=30).splitlines()
        return parse_cpu_sample(lines)

    def clock(self):
        """This machine's clock against the caller's: (lowest, highest possible offset in seconds, what its NTP client
        says its own offset is). The round trip of one `date` bounds the offset from both sides; (None, None, "") when
        the machine does not answer."""
        t0 = time.time()
        raw = self.out("date +%s.%N", timeout=30)
        t1 = time.time()
        try:
            remote = float(raw)
        except ValueError:
            return None, None, ""
        ntp = self.out("timeout 5 timedatectl timesync-status 2>/dev/null | sed -n 's/^ *Offset: *//p'", timeout=30)
        return round(remote - t1, 3), round(remote - t0, 3), ntp.strip()


def parse_cpu_sample(lines):
    """The output of Host.cpu_sample's command: CLK_TCK, the aggregate cpu line, then '<pid> <stat>' per pid."""
    if len(lines) < 2:
        return None, None, {}
    tick = int(lines[0]) if lines[0].strip().isdigit() else 100
    v = [int(x) for x in lines[1].split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    per = {}
    for line in lines[2:]:
        pid, _, stat = line.partition(" ")
        try:
            f = stat.rsplit(")", 1)[1].split()
            per[int(pid)] = (int(f[11]) + int(f[12])) / tick
        except (IndexError, ValueError):
            if pid.strip().isdigit():
                per[int(pid)] = None
    return sum(v) - idle, sum(v), per


# -- a machine's counters over a phase -------------------------------------------------------------------------------
# Interfaces that are not the machine's wire: loopback, container plumbing, tunnels. What is left (eth0, eth1, ens3...)
# is what the machine exchanged with other machines.
NOT_WIRE = ("lo", "veth", "docker", "br-", "wg", "tun", "virbr", "cni", "dummy")
UDP_ERRORS = ("InErrors", "RcvbufErrors", "SndbufErrors", "MemErrors")


def sampler_cmd(pids, interval, max_s):
    """The remote loop: every `interval` s one sample of the machine clock (T), /proc/stat's cpu line (C), each pid's
    stat (P), /proc/net/dev (N) and the Udp lines of /proc/net/snmp (U), closed by E. Shell builtins write the samples,
    so the shell dies of SIGPIPE when the caller goes away, and the loop ends by itself after max_s whatever happens
    (an orphaned sampler once polled a host for days)."""
    pids = " ".join(str(int(p)) for p in pids if p)
    per_pid = (f"for p in {pids}; do {{ read -r l < /proc/$p/stat; }} 2>/dev/null && printf 'P %s %s\\n' \"$p\" \"$l\"; done; "
               if pids else "")
    return ("printf 'K %s\\n' \"$(getconf CLK_TCK)\"; end=$(( $(date +%s) + " + str(int(max_s)) + " )); "
            "while [ \"$(date +%s)\" -lt \"$end\" ]; do printf 'T %s\\n' \"$(date +%s.%N)\"; "
            "read -r l < /proc/stat; printf 'C %s\\n' \"$l\"; " + per_pid +
            "while read -r l; do printf 'N %s\\n' \"$l\"; done < /proc/net/dev; "
            "while read -r l; do case \"$l\" in Udp:*) printf 'U %s\\n' \"$l\";; esac; done < /proc/net/snmp; "
            f"printf 'E\\n'; sleep {float(interval)}; done")


def parse_machine_samples(lines):
    """The sampler's output as a list of samples: {"t", "busy", "total" (jiffies over all cores), "pid": {pid: cpu
    seconds}, "net": {iface: (rx bytes, tx bytes)}, "udp": {counter: value}}. A sample cut short at the end is dropped."""
    tick, out, cur, udp_head = 100, [], None, None
    for line in lines:
        tag, _, rest = line.rstrip("\n").partition(" ")
        try:
            if tag == "K":
                tick = int(rest) if rest.strip().isdigit() else 100
            elif tag == "T":
                cur = {"t": float(rest), "busy": None, "total": None, "pid": {}, "net": {}, "udp": {}}
            elif cur is None:
                continue
            elif tag == "C":
                v = [int(x) for x in rest.split()[1:]]
                idle = v[3] + (v[4] if len(v) > 4 else 0)
                cur["busy"], cur["total"] = sum(v) - idle, sum(v)
            elif tag == "P":
                pid, _, stat = rest.partition(" ")
                f = stat.rsplit(")", 1)[1].split()
                cur["pid"][int(pid)] = (int(f[11]) + int(f[12])) / tick
            elif tag == "N" and ":" in rest:
                name, nums = rest.split(":", 1)
                f = nums.split()
                cur["net"][name.strip()] = (int(f[0]), int(f[8]))
            elif tag == "U":
                f = rest.split()[1:]
                if f and not f[0].lstrip("-").isdigit():
                    udp_head = f
                elif udp_head:
                    cur["udp"] = {k: int(v) for k, v in zip(udp_head, f)}
            elif tag == "E":
                out.append(cur)
                cur = None
        except (ValueError, IndexError):
            continue
    return out


def wire_bytes(sample):
    """(rx, tx) summed over the machine's wire interfaces (everything but NOT_WIRE)."""
    rx = tx = 0
    for name, (r, t) in sample["net"].items():
        if not name.startswith(NOT_WIRE):
            rx, tx = rx + r, tx + t
    return rx, tx


def counter_at(samples, t, get):
    """A cumulative counter at machine time t, linear between samples and held at the ends; None when unknown."""
    pts = [(s["t"], get(s)) for s in samples]
    pts = [(x, v) for x, v in pts if v is not None]
    if not pts:
        return None
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            return v0 + (v1 - v0) * ((t - t0) / (t1 - t0) if t1 > t0 else 1)
    return pts[-1][1]


def machine_window(samples, a, b, pids=()):
    """What the machine did between its own clock's a and b (clipped to the stretch the samples cover): busy % of all
    cores, each pid's CPU as % of one core, wire bytes in and out, the UDP error counters' growth. None for every
    figure the samples cannot give; "covered_s" says how much of [a, b] they span."""
    out = {"cpu_pct": None, "pid_pct": {int(p): None for p in pids if p}, "wire_rx": None, "wire_tx": None, "udp_errors": None,
           "covered_s": 0.0}
    if len(samples) < 2:
        return out
    a, b = max(a, samples[0]["t"]), min(b, samples[-1]["t"])
    if b <= a:
        return out
    out["covered_s"] = round(b - a, 2)

    def delta(get):
        x, y = counter_at(samples, a, get), counter_at(samples, b, get)
        return None if x is None or y is None else y - x

    busy, total = delta(lambda s: s["busy"]), delta(lambda s: s["total"])
    if busy is not None and total:
        out["cpu_pct"] = round(100 * busy / total, 1)
    for p in out["pid_pct"]:
        d = delta(lambda s, p=p: s["pid"].get(p))
        out["pid_pct"][p] = None if d is None else round(100 * d / (b - a), 1)
    rx, tx = delta(lambda s: wire_bytes(s)[0]), delta(lambda s: wire_bytes(s)[1])
    out["wire_rx"], out["wire_tx"] = (None if rx is None else int(rx)), (None if tx is None else int(tx))
    errs = {k: delta(lambda s, k=k: s["udp"].get(k)) for k in UDP_ERRORS}
    if any(v is not None for v in errs.values()):
        out["udp_errors"] = {k: int(v) for k, v in errs.items() if v is not None}
    return out


class Sampler:
    """One machine's sample stream for the length of a phase: start() before it, stop() after it, then `samples`."""

    def __init__(self, host, pids=(), interval=2.0, max_s=900):
        self.host, self.pids, self.interval, self.max_s = host, [p for p in pids if p], interval, max_s
        self.proc, self.lines, self.samples, self._reader = None, [], [], None

    def start(self):
        self.proc = subprocess.Popen(self.host.argv(sampler_cmd(self.pids, self.interval, self.max_s)), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
        self._reader = threading.Thread(target=lambda: self.lines.extend(self.proc.stdout), daemon=True)
        self._reader.start()
        return self

    def wait_first(self, timeout=10):
        """True once the first sample has arrived (the ssh session is up and the loop is running)."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if any(line.startswith("E") for line in list(self.lines)):
                return True
            time.sleep(0.1)
        return False

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self._reader.join(timeout=10)
            self.proc.stdout.close()
            self.proc = None
        self.samples = parse_machine_samples(self.lines)
        return self.samples
