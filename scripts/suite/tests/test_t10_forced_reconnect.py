"""T10-forced-reconnect (gate): during a call (callprobe) the WireGuard peer is removed on the exit server at
+T_KILL s; arm T = far end keeps streaming, arm S = far end pauses while we are silent. Records time to recovery
(first downstream packet after the kill), DecapStalled, rebinds.
Pass: recovery within RECOVER_MAX s in both arms, zero DecapStalled."""
import random
import time

from suitelib import shell
from suitelib.client import connect_or_fail
from suitelib.config import q

TEST = "T10-forced-reconnect"
KIND = "gate"
KNOBS = dict(DUR=q(300, 150), T_KILL=60, RECOVER_MAX=90, REPEATS=q(3, 1))
TIMEOUT = lambda k: 2 * k.REPEATS * (k.DUR + 400)   # seconds; the harness fails the test past this


def recovery_s(csv_path, t_kill):
    """Seconds from the kill to the first downstream packet more than 2 s after it, or None."""
    try:
        for line in open(csv_path):
            p = line.strip().split(",")
            if p[0] == "R":
                t = float(p[-1])
                if t > t_kill + 2:
                    return round(t - t_kill, 1)
    except (FileNotFoundError, ValueError):
        pass
    return None


def server_peer_for(server, tun_ip):
    """The public key of the wggvpn peer whose allowed-ips hold tun_ip, from `wg show wggvpn dump` on the exit."""
    if not tun_ip:
        return ""
    dump = shell.out(["docker", "exec", server, "wg", "show", "wggvpn", "dump"], timeout=30)
    for line in dump.splitlines()[1:]:
        f = line.split("\t")
        if len(f) > 3 and any(a.split("/")[0] == tun_ip for a in f[3].split(",")):
            return f[0]
    return ""


def test_forced_reconnect(cfg, run, client, target, checks, knobs):
    k = knobs
    if not shell.ok(["docker", "container", "inspect", cfg.server], timeout=30):
        checks.skip(f"needs the exit server container {cfg.server} to remove the WireGuard peer on")
    for arm in ("T", "S"):
        for rep in range(1, k.REPEATS + 1):
            s = connect_or_fail(checks, client, cfg.dest, 15)
            if not s:
                return
            with s:
                # OUR peer on the server, resolved before anything starts: never every peer, other clients (T22's
                # extras) may be connected and their sessions are not this test's subject. The client's WireGuard is a
                # userspace implementation over a TUN device, so `wg show` inside the container sees nothing; the peer
                # is identified on the server by its allowed-ips, which is the client's own tunnel address.
                tun_ip = client.out(f"ip -4 -o addr show dev {s.iface} | awk '{{print $4}}' | cut -d/ -f1")
                pub = server_peer_for(cfg.server, tun_ip)
                if not pub:
                    checks.failed(f"arm {arm} rep {rep}: no peer on the exit's wggvpn has allowed-ips {tun_ip or '?'} (the client's tunnel address)")
                    continue
                sid = random.getrandbits(30)
                out = f"t10-{arm}-{rep}"
                t_start = time.time()
                client.probe_bg("callprobe", out, host=target.ip, port=target.call_port, sid=sid, duration=k.DUR, iface=s.iface,
                                idle_pause=(arm == "S"))
                time.sleep(k.T_KILL)
                t_kill = time.time()
                r = shell.run(["docker", "exec", cfg.server, "wg", "set", "wggvpn", "peer", pub, "remove"], timeout=60)
                if r.returncode != 0:
                    checks.failed(f"arm {arm} rep {rep}: removing peer {pub[:12]}... on the server failed: {r.stderr.strip()[:120]}")
                    time.sleep(max(0, k.DUR + 10 - (time.time() - t_start)))   # let the probe end; no probe leaks into the next rep
                    continue
                time.sleep(max(0, k.DUR + 20 - (time.time() - t_start)))
                e = s.errors()
                s.save_log(out)
            rec = recovery_s(run / f"{out}.csv", t_kill)
            j = run.read_json(f"{out}.json", {})
            checks.row(arm=arm, rep=rep, recovery_s=rec if rec is not None else "never", errors=e, call=j)
            recd = "NEVER recovered" if rec is None else f"recovered {rec}s after the kill"
            msg = (f"arm {arm} rep {rep}: downstream {recd}; DecapStalled {e['decap_stalled']}; reconnects {e['reconnects']}; "
                   f"rebinds {j.get('rebinds')}")
            checks.verdict(rec is not None and rec <= k.RECOVER_MAX and e["decap_stalled"] == 0, msg)
