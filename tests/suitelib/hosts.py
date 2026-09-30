"""Where a command runs: this machine, or another one over ssh (the multi-machine testenv, tests/multihost.py).

A Host is built from an ssh target ("root@10.114.0.16") or one of LOCAL for this machine. Every call has a timeout
(shell.run). cpu_sample() reads /proc on that machine, so CPU is measured where the process runs."""
import shlex

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
