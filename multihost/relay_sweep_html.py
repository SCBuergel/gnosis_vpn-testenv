#!/usr/bin/env python3
"""One self-contained HTML report of a relay sweep directory (multihost/relay_sweep.py):

    python3 multihost/relay_sweep_html.py ~/gnosis_vpn-testenv-logs/relay-sweep/sw1 [out.html]

Reads runs/<name>-<label>/ (rows.jsonl, probe JSONs, console.log), vmstat/<label>/, specs/, machines.csv and the
sweep state; embeds the data as JSON and draws the charts in the page (no external scripts)."""
import csv
import datetime as dt
import glob
import html
import json
import math
import re
import statistics as st
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relay_sweep import KNOBS, RELAYS  # noqa: E402
from relay_sweep_report import interp, pct  # noqa: E402

KIND = {"c": "CPU-optimized (dedicated)", "s": "Basic (shared, regular)"}


def r1(x, d=1):
    return None if x is None else round(x, d)


def vmstat(path):
    """[[epoch, r, free MB, us, sy, id, wa, st]] from `vmstat -n -t 1` (UTC stamps)."""
    out = []
    for line in Path(path).read_text().splitlines():
        p = line.split()
        if len(p) < 19 or not p[0].isdigit():
            continue
        try:
            t = dt.datetime.strptime(p[-2] + " " + p[-1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            continue
        out.append([t, int(p[0]), round(int(p[3]) / 1024), int(p[12]), int(p[13]), int(p[14]), int(p[15]), int(p[16])])
    return out


def vm_summary(v):
    if not v:
        return None
    busy = [100 - x[5] for x in v]
    return {"samples": len(v), "busy_mean": r1(st.mean(busy)), "busy_p95": pct(busy, 95), "busy_max": max(busy),
            "us_mean": r1(st.mean(x[3] for x in v)), "sy_mean": r1(st.mean(x[4] for x in v)), "wa_max": max(x[6] for x in v),
            "st_mean": r1(st.mean(x[7] for x in v), 2), "st_max": max(x[7] for x in v), "runq_mean": r1(st.mean(x[1] for x in v)),
            "runq_max": max(x[1] for x in v), "free_min_mb": min(x[2] for x in v), "free_mean_mb": round(st.mean(x[2] for x in v))}


def main(root, out):
    root = Path(root).expanduser()
    name = root.name
    state = json.loads((Path.home() / ".gvpn-fleets" / f"{name}-sweep.json").read_text())
    machines = list(csv.DictReader(open(root / "machines.csv")))
    specs_raw = {Path(f).stem: Path(f).read_text() for f in sorted(glob.glob(str(root / "specs" / "*.txt")))}
    timeline = {}
    for logf in ("sw1-run.log", "sw1-run2.log"):
        p = root.parent / logf
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            m = re.match(r"(\d\d:\d\d:\d\d) relay (\w+): (.*)", line)
            if m:
                ts, lab, msg = m.groups()
                key = {"bringing": "swap_start", "test run": "test_start", "done": "test_end"}
                for k, v in key.items():
                    if msg.startswith(k):
                        timeline.setdefault(lab, {})[v] = ts
    relays = []
    for label, size in RELAYS:
        run = root / "runs" / f"{name}-{label}"
        if not run.is_dir():
            continue
        rows = [json.loads(x) for x in (run / "rows.jsonl").read_text().splitlines() if x.strip()]
        topo = next((r for r in rows if r.get("kind") == "topology"), {})
        rungs = {r["n"]: r for r in rows if r.get("kind") == "rung"}
        clients = [r for r in rows if r.get("kind") == "client"]
        console = (run / "console.log").read_text() if (run / "console.log").exists() else ""
        fails = [re.sub(r"\s+", " ", x)[:400] for x in re.findall(r"^FAIL T34-single-relay-scaling: (.*)$", console, re.M)]
        rung_t = {}
        for m in re.finditer(r"^(\d\d:\d\d:\d\d) n=(\d+): channels ok", console, re.M):
            rung_t[int(m.group(2))] = m.group(1)
        hosts = tomllib.loads((root / "runs" / f"hosts-test-{label}.toml").read_text()) if (root / "runs" / f"hosts-test-{label}.toml").exists() else {}
        ssh2name = {}
        for role in ("relays", "exits", "clients"):
            for mm in hosts.get("roles", {}).get(role, {}).get("machines", []):
                ssh2name[mm["ssh"]] = f"gvpn-{name}-{mm['name']}"
        # per second per rung, per client and aggregate
        per = {}
        windows = []
        for n in sorted(rungs):
            probes = []
            for f in sorted(run.glob(f"t34-n{n}-down-*.json")):
                p = json.loads(f.read_text())
                cname = f.stem.split("-down-", 1)[1]
                k = 1 if cname == "gnosis_vpn-client" else int(cname.rsplit("-", 1)[1])
                probes.append((k, p))
            if not probes:
                continue
            t0 = min(p["start"] for _, p in probes if p.get("start"))
            series = {}
            agg = {}
            end_all = t0
            for k, p in sorted(probes):
                s = p.get("samples") or []
                end = max([x[0] for x in s] + [p.get("done") or 0, t0])
                end_all = max(end_all, end)
                by = {}
                for seq, sent, rtt in p.get("pings") or []:
                    by.setdefault(int(math.floor(sent - t0)), []).append(rtt)
                pts = []
                for sec in range(int(math.ceil(end - t0))):
                    mb = (interp(s, t0 + sec + 1) - interp(s, t0 + sec)) * 8 / 1e6
                    pr = by.get(sec, [])
                    got = [x for x in pr if x is not None]
                    ping = round(got[0], 1) if got else (-1 if pr else None)      # -1: echo sent, no reply
                    pts.append([round(mb, 2), ping])
                    a = agg.setdefault(sec, [0.0, [], 0, 0])
                    a[0] += mb
                    a[1] += got
                    a[2] += len(pr)
                    a[3] += len(pr) - len(got)
                series[k] = pts
            per[n] = {"t0": t0, "clients": series,
                      "agg": [[sec, round(a[0], 2), round(st.mean(a[1]), 1) if a[1] else None, a[2], a[3]] for sec, a in sorted(agg.items())]}
            windows.append([n, round(t0, 1), round(end_all, 1)])
        # rung summary
        summ = []
        for n, r in sorted(rungs.items()):
            rtts, lost = [], 0
            for pts in (per.get(n) or {}).get("clients", {}).values():
                for _, ping in pts:
                    if ping is None:
                        continue
                    if ping < 0:
                        lost += 1
                    else:
                        rtts.append(ping)
            proc = [v for v in (r.get("ut_process_cpu_down") or {}).values() if v is not None]
            udp = r.get("ut_udp_errors_down") or {}
            wire = r.get("ut_wire_down") or [None, None]
            cnt = next(iter((r.get("ut_counts_down") or {}).values()), {}) or {}
            mcpu = {ssh2name.get(k, k): v for k, v in (r.get("machine_cpu_down_pct") or {}).items()}
            summ.append({"n": n, "agg": r.get("down_agg_mbit"), "mean": r.get("down_avg_mbit"), "min": r.get("down_min_mbit"),
                         "complete": n - (r.get("incomplete_down") or 0), "slowest_s": r.get("down_slowest_s"), "window_s": r.get("down_window_s"),
                         "buckets": r.get("down_bucket_mbit"), "stall_max_s": r.get("down_stall_max_s"),
                         "ping_med": r1(st.median(rtts)) if rtts else None, "ping_p95": r1(pct(rtts, 95)) if rtts else None,
                         "ping_loss": r1(100 * lost / (lost + len(rtts))) if lost + len(rtts) else None, "pings": lost + len(rtts),
                         "relay_cpu": r.get("ut_machine_cpu_down"), "relay_proc": proc[0] if proc else None,
                         "udp_rcvbuf": udp.get("RcvbufErrors"), "udp_sndbuf": udp.get("SndbufErrors"), "udp_in": udp.get("InErrors"),
                         "wire_rx_mb": r1(wire[0] / 1e6) if wire[0] else None, "wire_tx_mb": r1(wire[1] / 1e6) if wire[1] else None,
                         "forwarded": cnt.get("forwarded"), "frames_discarded": r.get("frames_discarded"), "reconnects": r.get("reconnects"),
                         "ping_timeouts": r.get("ping_timeouts"), "connect_ms": r.get("connect_ms"), "start": rung_t.get(n),
                         "exits_cpu": (r.get("host_cpu_down_pct") or {}).get("exits"), "clients_cpu": (r.get("host_cpu_down_pct") or {}).get("clients"),
                         "exits_cpu_max": (r.get("host_cpu_down_max") or {}).get("exits"), "clients_cpu_max": (r.get("host_cpu_down_max") or {}).get("clients"),
                         "machine_cpu": mcpu})
        cl = []
        for c in clients:
            d = c.get("down") or {}
            tun = d.get("tunnel") or {}
            k = 1 if c["client"] == "gnosis_vpn-client" else int(c["client"].rsplit("-", 1)[1])
            cl.append({"n": c["n"], "k": k, "client": f"client-{k}", "exit": f"exit-{k}", "bytes_mb": r1((d.get("bytes") or 0) / 1e6),
                       "complete": d.get("complete"), "error": d.get("error"), "secs": r1((d.get("last") or 0) - (d.get("first") or 0)) if d.get("first") else None,
                       "overlap_mbit": c.get("down_overlap_mbit"), "stall_s": c.get("down_stall_s"), "tunnel_rx_mb": r1((tun.get("rx") or 0) / 1e6),
                       "reconnects": (c.get("errors") or {}).get("reconnects"), "ping_timeouts": (c.get("errors") or {}).get("ping_timeouts"),
                       "frames_discarded": (c.get("errors") or {}).get("frame_discarded"), "warn_error_lines": (c.get("errors") or {}).get("warn_error_lines")})
        missing = sorted(set(range(1, 7)) - set(rungs))
        # vmstat
        vm = {}
        for f in sorted(glob.glob(str(root / "vmstat" / label / "*.txt"))):
            v = vmstat(f)
            vm[Path(f).stem] = v
        relay_name = state["relays"].get(label, {}).get("name") or f"gvpn-{name}-relay-{label}"
        relay_vm = vm.get(relay_name, [])
        if windows and relay_vm:
            a, b = windows[0][1] - 30, windows[-1][2] + 30
            relay_vm = [x for x in relay_vm if a <= x[0] <= b]
        vm_sum = {}
        for mname, v in vm.items():
            if windows:
                v = [x for x in v if windows[0][1] <= x[0] <= windows[-1][2]]
            vm_sum[mname] = vm_summary(v)
        tl = timeline.get(label, {})
        relays.append({"label": label, "size": size, "kind": KIND[label[0]], "droplet": relay_name,
                       "droplet_id": state["relays"].get(label, {}).get("id"), "summary": summ, "clients": cl, "fails": fails,
                       "missing": missing, "per": per, "windows": windows,
                       "relay_vm": [[round(x[0] - (windows[0][1] if windows else 0)), x[3], x[4], x[7], x[2], x[1]] for x in relay_vm],
                       "vm_sum": vm_sum, "timeline": tl, "stack": topo.get("stack", {}), "verdict": "passed" if not fails else "failed"})
    data = {"name": name, "relays": relays, "machines": machines, "knobs": KNOBS,
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    tpl = (Path(__file__).resolve().parent / "relay_sweep_html.tpl").read_text()
    specs_html = "\n".join(f'<details><summary>{html.escape(k)}</summary><pre>{html.escape(v)}</pre></details>' for k, v in specs_raw.items())
    page = tpl.replace("/*DATA*/null", json.dumps(data, separators=(",", ":"))).replace("<!--SPECS-->", specs_html)
    Path(out).write_text(page)
    print(f"{out}: {len(page) / 1e6:.1f} MB")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else str(Path(sys.argv[1]).expanduser() / f"relay-sweep-{Path(sys.argv[1]).name}.html"))
