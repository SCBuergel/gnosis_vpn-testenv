"""T20-fault-injection (diagnostic): what happens to a live session when a relay degrades or disappears? During a
call: a tc netem loss ladder over LOSSES (%) on the host toward relay RELAY's P2P port, STEP_S per step, then a
full pause of the relay process (SIGSTOP for STEP_S), then restore. The call lasts (1 + len(LOSSES) + 3) x STEP_S
(until 2026-09-20 it was computed from the length of the LOSSES string and outran the script). SKIP unless root,
SKIP when the cluster status carries no pid for RELAY (a SIGSTOP of pid 0 stops the runner's own process group),
and SKIP naming the step when a tc qdisc or filter cannot be installed (an unimpaired tunnel would survive and
pass).

Each rung's 'tc qdisc change' is checked and recorded (applied per row); WARN when the session survived but a rung
was not applied. Otherwise PASS iff the client is still connected after the ladder and the pause; a miss is WARN.
Call loss, stalls and reconnects are recorded.

Why: T09 varies latency and membership between sessions; nothing else touches a fault arriving mid-session, the
real-world case. Upstream documents it as hazardous both ways: killing a return relay can collapse even a 0-hop
forward direction, and the automatic recovery is destructive on a false positive."""
import os
import signal
import time

from suitelib import shell
from suitelib.client import connect_or_fail
from suitelib.config import q

TEST = "T20-fault-injection"
KIND = "diagnostic"
KNOBS = dict(LOSSES="1 5 20", STEP_S=q(60, 30), RELAY=1)
TIMEOUT = lambda k: (len(k.words("LOSSES")) + 5) * k.STEP_S + 900   # seconds; the harness fails the test past this


def test_fault_injection(cfg, run, client, live_cluster, target, checks, knobs):
    cluster = live_cluster
    k = knobs
    if os.geteuid() != 0:
        checks.skip("needs root for tc")
    port = cluster.p2p_port(k.RELAY)
    iface = cfg.netem_iface
    relay_pid = cluster.pid(k.RELAY)
    if relay_pid <= 0:
        checks.skip(f"no pid for relay node {k.RELAY} in the cluster status (SIGSTOP would hit pid 0 = this process group)")
    tc = lambda *a: shell.run(["tc", *a], timeout=30)   # noqa: E731

    def cleanup():
        tc("qdisc", "del", "dev", iface, "root")
        try:
            os.kill(relay_pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError):
            pass

    try:
        for label, step in (("root prio qdisc", ("qdisc", "add", "dev", iface, "root", "handle", "1:", "prio")),
                            ("netem qdisc", ("qdisc", "add", "dev", iface, "parent", "1:3", "handle", "30:", "netem", "loss", "0%")),
                            ("port filter", ("filter", "add", "dev", iface, "protocol", "ip", "parent", "1:0", "prio", "1", "u32", "match", "ip",
                                             "dport", str(port), "0xffff", "flowid", "1:3"))):
            r = tc(*step)
            if r.returncode != 0:
                # an unimpaired tunnel would still "survive" and PASS; say why nothing was measured instead
                checks.skip(f"tc setup failed ({label}): {(r.stderr or r.stdout).strip()[:160]}")
        s = connect_or_fail(checks, client, cfg.dest, 15)
        if not s:
            return
        losses = k.words("LOSSES")
        # the call must cover the whole ladder: (1 settle + one step per loss value + 1 pause + 2 restore) x STEP_S
        total = (len(losses) + 1) * k.STEP_S + 3 * k.STEP_S
        with s:
            client.probe_bg("relprobe", "t20-call", host=target.ip, port=target.echo_port, rate_mbit=1.5, duration=total, size=1200, iface=s.iface)
            time.sleep(k.STEP_S)
            applied = []
            for l in losses:
                r = tc("qdisc", "change", "dev", iface, "parent", "1:3", "handle", "30:", "netem", "loss", f"{l}%")
                ok_step = r.returncode == 0
                applied.append(ok_step)
                checks.row(arm=f"loss{l}", t=int(time.time()), applied=ok_step)
                if not ok_step:
                    checks.record(f"loss {l}% rung NOT applied ({(r.stderr or r.stdout).strip()[:120]}); the rung measured the previous level")
                time.sleep(k.STEP_S)
            tc("qdisc", "change", "dev", iface, "parent", "1:3", "handle", "30:", "netem", "loss", "0%")
            os.kill(relay_pid, signal.SIGSTOP)
            checks.row(arm="pause", t=int(time.time()))
            time.sleep(k.STEP_S)
            os.kill(relay_pid, signal.SIGCONT)
            checks.row(arm="restore", t=int(time.time()))
            time.sleep(k.STEP_S * 2)
            e = s.errors()
            s.save_log("t20")
            still = client.is_connected()
            for _ in range(30):   # the probe writes its report at the end of its duration
                if (run / "t20-call.json").exists() and (run / "t20-call.json").stat().st_size > 0:
                    break
                time.sleep(1)
        j = run.read_json("t20-call.json", {})
        checks.row(kind="summary", call=j, errors=e, session_survived=still, relay=k.RELAY, port=port)
        if still and not all(applied):
            checks.failed(f"session survived, but {applied.count(False)} of {len(applied)} loss rungs were not applied; the ladder was not the one configured")
        elif still:
            checks.passed(f"session survived loss ladder [{k.LOSSES}]% and a {k.STEP_S}s relay pause; call loss {j.get('loss_pct')}%, "
                          f"stalls>5s {j.get('stalls_gt_5s')}, reconnects {e['reconnects']}")
        else:
            checks.failed(f"session died during fault injection (reconnects {e['reconnects']})")
    finally:
        cleanup()
