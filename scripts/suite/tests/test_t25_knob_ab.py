"""T25-knob-ab (runbook): restart the cluster with one env var changed (KNOB="K=V"), everything else untouched,
T04 both ways. CLIENT_KNOB applies K=V to the client instead (e.g. GNOSISVPN_SURB_RAMP_SECS=0). Informational:
records both cells' medians."""
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

    a = cell("baseline")
    if k.CLIENT_KNOB:
        shell.run("just client-stop", timeout=120, cwd=cwd)
        shell.run("just client-start", timeout=300, cwd=cwd, env={**os.environ, "CLIENT_EXTRA_ENV": k.CLIENT_KNOB})
    else:
        if not shell.ok("just cluster-restart", timeout=1800, cwd=cwd, env={**os.environ, "CLUSTER_ENV": k.KNOB}):
            checks.record(f"cluster restart with {k.KNOB} failed")
            return
    client.wait_worker(180)
    b = cell("knob")
    if k.CLIENT_KNOB:
        shell.run("just client-stop", timeout=120, cwd=cwd)
        shell.run("just client-start", timeout=300, cwd=cwd)
    else:
        shell.run("just cluster-restart", timeout=1800, cwd=cwd)
    client.wait_worker(180)
    checks.record(f"baseline down {a.get('down_median')} up {a.get('up_median')}; with {k.CLIENT_KNOB or k.KNOB}: "
                  f"down {b.get('down_median')} up {b.get('up_median')} Mbit/s")
