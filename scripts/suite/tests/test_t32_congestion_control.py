"""T32-congestion-control (runbook): client-host TCP congestion control A/B, cubic vs bbr (+fq) set inside the
client's network namespace (docker --sysctl, restart per arm), ABBA order over PAIRS pairs; upload is the treated
direction, download the null-direction control. Needs tcp_bbr on the host. Reports paired ratios and sign counts."""
import os
import statistics as st

from suitelib import shell
from suitelib.client import ConnectFailed
from suitelib.config import q
from suitelib.target import curl_down, curl_up

TEST = "T32-congestion-control"
KIND = "runbook"
KNOBS = dict(PAIRS=q(6, 3))


def test_congestion_control(cfg, client, target, checks, knobs):
    try:
        avail = open("/proc/sys/net/ipv4/tcp_available_congestion_control").read().split()
    except OSError:
        avail = []
    if "bbr" not in avail:
        checks.skip("tcp_bbr not available on the host (modprobe tcp_bbr)")
    cwd = str(cfg.testenv_dir)

    def restart_cc(cc):
        shell.run("just client-stop", timeout=120, cwd=cwd)
        shell.run("just client-start", timeout=300, cwd=cwd, env={**os.environ, "CLIENT_SYSCTL": f"net.ipv4.tcp_congestion_control={cc}"})
        client.wait_worker(180)

    def measure():
        try:
            s = client.connect(cfg.dest, 15)
        except ConnectFailed:
            return (0.0, 0.0)
        with s:
            cc = client.out("cat /proc/sys/net/ipv4/tcp_congestion_control")
            d = curl_down(client, target.ip, cfg.bytes, cfg.cap)
            u = curl_up(client, target.ip, cfg.bytes, cfg.cap)
        checks.row(cc=cc, down=d, up=u)
        return (d["mbit"], u["mbit"])

    a, b = [], []
    for p in range(1, knobs.PAIRS + 1):
        for cc in (("cubic", "bbr") if p % 2 else ("bbr", "cubic")):
            restart_cc(cc)
            (a if cc == "cubic" else b).append(measure())
    shell.run("just client-stop", timeout=120, cwd=cwd)
    shell.run("just client-start", timeout=300, cwd=cwd)
    client.wait_worker(180)

    def cmp(idx):
        ra = [y[idx] / x[idx] for x, y in zip(a, b) if x[idx] > 0]
        return {"median_ratio_bbr_over_cubic": round(st.median(ra), 3) if ra else None, "bbr_faster": sum(1 for r in ra if r > 1), "n": len(ra)}

    res = {"upload": cmp(1), "download_control": cmp(0), "pairs": min(len(a), len(b))}
    checks.row(kind="summary", result=res)
    up, dn = res["upload"], res["download_control"]
    checks.passed(f"upload bbr/cubic {up['median_ratio_bbr_over_cubic']} ({up['bbr_faster']}/{up['n']} bbr faster); download control "
                  f"{dn['median_ratio_bbr_over_cubic']} ({dn['bbr_faster']}/{dn['n']})")
