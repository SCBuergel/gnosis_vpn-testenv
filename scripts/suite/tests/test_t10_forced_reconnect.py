"""T10-forced-reconnect (gate): when the tunnel is torn down mid-call and rebuilt, does the fresh session survive
traffic already arriving, and how long is the user dark? A callprobe call of DUR s; at +T_KILL s the client's
WireGuard peer is removed on the exit server, found in 'wg show wggvpn dump' by allowed-ips = the client's tunnel
address (the client's WireGuard is userspace, so nothing shows inside its container), never every peer. Arm T: the
far end keeps streaming through the kill; arm S: it pauses while the client is silent and resumes on its next
packet. REPEATS per arm. Records time to the first downstream packet after the kill, DecapStalled, rebinds.
DUR - T_KILL must exceed RECOVER_MAX: the client notices a removed peer only through three liveness-ping cycles,
about 75 s, so a 70 s window reads as never recovered.

Pass iff, per arm and repeat, the first downstream packet after the removal arrives within RECOVER_MAX s and
DecapStalled = 0; a repeat whose peer key cannot be read or whose removal fails FAILs. Reconnects and rebinds are
recorded. Calibration: 72-83 s on the reference stack.

Why: this is the user's 'intermittent loss of connection'. On 2026-09-14 every one of ten reconnects died 3-7 s
after 'session is ready' (DecapStalled), then cost a 2-minute ping timeout and a worker restart, about 70 % of the
hour lost; the quiet-restart arm had no failures. T07 covers only the first connect and T23 reaches a reconnect
only by accident."""
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
                tun_ip = client.out(f"ip -4 -o addr show dev {s.iface} | head -1 | awk '{{print $4}}' | cut -d/ -f1")
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
