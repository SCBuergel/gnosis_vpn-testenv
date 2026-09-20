"""The in-cluster traffic target and the transfers against it. The target sits on its own Docker network with
a non-private subnet (the client keeps RFC1918 off the tunnel); the exit NATs into it. `ip` is the address
reached through the exit, `ip_direct` the one on the client's own network for no-VPN baselines."""
import json
import statistics as st

from . import shell


class Target:
    http_port, echo_port, stream_port, call_port = 8899, 8901, 8902, 8903

    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg.target_name
        self.ip = self._ip(cfg.target_network)
        self.ip_direct = self._ip(cfg.docker_network)

    def _ip(self, network):
        raw = shell.out(["docker", "inspect", self.name], timeout=30)
        try:
            return json.loads(raw)[0]["NetworkSettings"]["Networks"][network]["IPAddress"]
        except (ValueError, KeyError, IndexError, TypeError):
            return ""

    def running(self):
        return bool(self.ip)

    def down_url(self, nbytes, host=None):
        return f"http://{host or self.ip}:{self.http_port}/down?bytes={nbytes}"

    def up_url(self, host=None):
        return f"http://{host or self.ip}:{self.http_port}/up"


def _curl_result(w, want, upload=False):
    p = (w.split() + ["0", "0", "0", "0"])[:4]
    code = p[0]
    b = int(float(p[1] or 0))
    t = float(p[2] or 0)
    ttfb = float(p[3] or 0)
    mbit = round(b * 8 / t / 1e6, 3) if t > 0 else 0
    complete = b >= want and (code == "200" if upload else True)
    return {"code": code, "bytes": b, "elapsed": round(t, 2), "ttfb": round(ttfb, 2), "mbit": mbit, "complete": complete}


def curl_down(client, host, nbytes, cap):
    """Sized download from the target inside the client: {code, bytes, elapsed, ttfb, mbit, complete}."""
    w = client.out(f"curl -s -o /dev/null -m {cap} -w '%{{http_code}} %{{size_download}} %{{time_total}} %{{time_starttransfer}}' "
                   f"'http://{host}:{Target.http_port}/down?bytes={nbytes}' 2>/dev/null || true", timeout=cap + 30)
    return _curl_result(w, nbytes)


def curl_up(client, host, nbytes, cap):
    client.exec(f"[ -f /tmp/up.bin ] && [ $(stat -c %s /tmp/up.bin) -eq {nbytes} ] || head -c {nbytes} /dev/zero > /tmp/up.bin", timeout=60)
    w = client.out(f"curl -s -o /dev/null -m {cap} -w '%{{http_code}} %{{size_upload}} %{{time_total}} %{{time_starttransfer}}' "
                   f"-H 'Content-Type: application/octet-stream' --data-binary @/tmp/up.bin "
                   f"'http://{host}:{Target.http_port}/up' 2>/dev/null || true", timeout=cap + 30)
    return _curl_result(w, nbytes, upload=True)


def transfer_series(checks, client, label, host, reps, nbytes, cap):
    """reps x (download, upload) with per-second stall detection; one row per transfer.
    Returns {down_median, up_median, down_complete, up_complete, reps, down: [...], up: [...]}."""
    down, up = [], []
    for r in range(1, reps + 1):
        client.persec_start(f"{label}-d{r}")
        d = curl_down(client, host, nbytes, cap)
        client.persec_stop()
        checks.row(label=label, dir="down", rep=r, res=d, stall_s=client.persec_stall(f"{label}-d{r}", "rx"))
        down.append(d)
        client.persec_start(f"{label}-u{r}")
        u = curl_up(client, host, nbytes, cap)
        client.persec_stop()
        checks.row(label=label, dir="up", rep=r, res=u, stall_s=client.persec_stall(f"{label}-u{r}", "tx"))
        up.append(u)
    return {"down_median": round(st.median([d["mbit"] for d in down]), 3) if down else 0,
            "up_median": round(st.median([u["mbit"] for u in up]), 3) if up else 0,
            "down_complete": sum(1 for d in down if d["complete"]),
            "up_complete": sum(1 for u in up if u["complete"]),
            "reps": len(down), "down": down, "up": up}


def summary_row(s):
    """The compact form of a transfer_series result for rows.jsonl."""
    return {k: s[k] for k in ("down_median", "up_median", "down_complete", "up_complete", "reps")}


def ping_rtts(client, host, count, interval=1, wait=3):
    """In-tunnel ping RTTs (ms) from the client."""
    txt = client.out(f"ping -c {count} -i {interval} -W {wait} {host} 2>/dev/null | grep -oE 'time=[0-9.]+' | cut -d= -f2",
                     timeout=count * (interval + wait) + 30)
    return [float(x) for x in txt.split()]


def ping_avg(client, host, count=5, interval=0.2, wait=2):
    """Average RTT (ms) from ping's summary line, or None."""
    txt = client.out(f"ping -c {count} -i {interval} -W {wait} {host} 2>/dev/null | sed -n 's|.*= \\([0-9.]*\\)/\\([0-9.]*\\)/.*|\\2|p'",
                     timeout=count * (interval + wait) + 30)
    try:
        return float(txt)
    except ValueError:
        return None
