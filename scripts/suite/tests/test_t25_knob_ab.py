"""T25-knob-ab (runbook): does one environment knob on one component explain a regression? Restart the cluster
with one env var changed (KNOB="K=V", default the relay decode concurrency at nproc x 8), everything else
untouched, and run REPS (--fast 2) T04-style transfers both ways; CLIENT_KNOB applies K=V to the client instead
(e.g. GNOSISVPN_SURB_RAMP_SECS=0). Nothing is asserted; both cells' medians are recorded. Every cluster node gets
the same env (per-role env is open extension 1).

Why: this is how both regressions were finally pinned (HOPR_INTERNAL_IN_PACKET_PIPELINE_CONCURRENCY was the hoprd
fix) and how a plausible suspect, the exit's LIFO SURB pop order, was cleared in one run. A negative result here is
as valuable as a positive one."""
import os

from suitelib import shell
from suitelib.client import ConnectFailed
from suitelib.target import summary_row, transfer_series

TEST = "T25-knob-ab"
KIND = "runbook"
KNOBS = dict(KNOB=f"HOPR_INTERNAL_IN_PACKET_PIPELINE_CONCURRENCY={(os.cpu_count() or 1) * 8}", CLIENT_KNOB="")


def test_knob_ab(cfg, client, target, checks, knobs):
    k = knobs
    cwd = str(cfg.testenv_dir)

    def cell(label):
        try:
            s = client.connect(cfg.dest, 15)
        except ConnectFailed as e:
            checks.record(f"{label}: connect failed: {e}")
            return {}
        with s:
            summ = transfer_series(checks, client, f"t25-{label}", target.ip, cfg.q(cfg.reps, 2), cfg.bytes, cfg.cap)
            e = s.errors()
        checks.row(arm=label, knob=k.KNOB, client_knob=k.CLIENT_KNOB, summary=summary_row(summ), errors=e)
        return summ

    def restart_client(extra_env, label):
        """A checked restart: a failed stop/start or a worker that never comes back abandons the A/B, recorded."""
        env = {**os.environ, "CLIENT_EXTRA_ENV": extra_env}
        ok = shell.ok("just client-stop", timeout=120, cwd=cwd) and shell.ok("just client-start", timeout=300, cwd=cwd, env=env)
        ok = ok and client.wait_worker(180)
        if not ok:
            checks.row(kind="summary", result={"abandoned": f"client restart for the {label} arm failed", "client_knob": k.CLIENT_KNOB})
            checks.record(f"client restart for the {label} arm ({extra_env or 'default env'}) failed; A/B abandoned")
        return ok

    a = cell("baseline")
    b = {}
    try:
        if k.CLIENT_KNOB:
            if not restart_client(k.CLIENT_KNOB, "knob"):
                return
        else:
            if not shell.ok("just cluster-restart", timeout=1800, cwd=cwd, env={**os.environ, "CLUSTER_ENV": k.KNOB}):
                checks.record(f"cluster restart with {k.KNOB} failed; A/B abandoned")
                return
            client.wait_worker(180)
        b = cell("knob")
    finally:
        # the default configuration comes back whatever happened above: an abandoned A/B must not leave the client
        # stopped or running with the knob (a runbook item that leaves the stack dead is the deadman rule again)
        if k.CLIENT_KNOB:
            restart_client("", "restore")
        else:
            shell.run("just cluster-restart", timeout=1800, cwd=cwd)
            client.wait_worker(180)
    checks.record(f"baseline down {a.get('down_median')} up {a.get('up_median')}; with {k.CLIENT_KNOB or k.KNOB}: "
                  f"down {b.get('down_median')} up {b.get('up_median')} Mbit/s")
