#!/usr/bin/env python3
"""Tables and CSVs from a relay sweep (multihost/relay_sweep.py) directory:

    python3 multihost/relay_sweep_report.py ~/gnosis_vpn-testenv-logs/relay-sweep/sw1

- per_second.csv: one row per client per second of every download: relay, rung, client, its machines, the second
  since the common start, the download rate in that second (Mbit/s), cumulative MB, the round-trip time of the echo
  sent in that second (empty when it got no reply: ping_lost=1);
- per_second_aggregate.csv: the same seconds summed over the rung's clients (rate) and pooled (ping);
- summary.csv and summary.md: per relay and rung, the aggregate and per-client rates over the overlap window (as
  T34-single-relay-scaling reports them), transfers complete, ping median / p95 / loss, relay CPU."""
import csv
import json
import math
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relay_sweep import RELAYS  # noqa: E402

LABELS = dict(RELAYS)


def interp(samples, t):
    if not samples or t <= samples[0][0]:
        return 0
    for (t0, b0), (t1, b1) in zip(samples, samples[1:]):
        if t0 <= t <= t1:
            return b0 + (b1 - b0) * (t - t0) / (t1 - t0) if t1 > t0 else b1
    return samples[-1][1]


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(math.ceil(p / 100 * len(v))) - 1)] if v else None


def main(root):
    root = Path(root).expanduser()
    state = json.loads((Path.home() / ".gvpn-fleets" / f"{root.name}-sweep.json").read_text())
    per, agg, summ = [], [], []
    for label, size in RELAYS:
        run = root / "runs" / f"{root.name}-{label}"
        if not run.is_dir():
            continue
        relay = state["relays"].get(label, {}).get("name", "")
        rows = [json.loads(x) for x in (run / "rows.jsonl").read_text().splitlines() if x.strip()]
        rungs = {r["n"]: r for r in rows if r.get("kind") == "rung"}
        for n in sorted(rungs):
            r = rungs[n]
            files = sorted(run.glob(f"t34-n{n}-down-*.json"))
            t0 = None
            probes = []
            for f in files:
                p = json.loads(f.read_text())
                client = f.stem.split("-down-", 1)[1]
                k = 1 if client == "gnosis_vpn-client" else int(client.rsplit("-", 1)[1])
                probes.append((k, client, p))
                if p.get("start"):
                    t0 = p["start"] if t0 is None else min(t0, p["start"])
            if t0 is None:
                continue
            secs = {}
            rtts = []
            lost = 0
            for k, client, p in sorted(probes):
                s = p.get("samples") or []
                end = max([x[0] for x in s] + [p.get("done") or 0, t0])
                pings = p.get("pings") or []
                by_sec = {}
                for seq, sent, rtt in pings:
                    by_sec.setdefault(int(math.floor(sent - t0)), []).append(rtt)
                    if rtt is None:
                        lost += 1
                    else:
                        rtts.append(rtt)
                for sec in range(0, int(math.ceil(end - t0))):
                    a, b = t0 + sec, t0 + sec + 1
                    mbit = (interp(s, b) - interp(s, a)) * 8 / 1e6
                    pr = by_sec.get(sec, [])
                    got = [x for x in pr if x is not None]
                    row = {"relay": label, "relay_size": size, "relay_droplet": relay, "clients": n, "client": client,
                           "client_droplet": f"gvpn-{root.name}-client-{k}", "exit_droplet": f"gvpn-{root.name}-exit-{k}",
                           "second": sec, "epoch": round(a, 3), "down_mbit_s": round(mbit, 3), "cum_mb": round(interp(s, b) / 1e6, 3),
                           "ping_ms": round(got[0], 2) if got else "", "ping_lost": int(bool(pr) and not got)}
                    per.append(row)
                    x = secs.setdefault(sec, {"mbit": 0.0, "rtt": [], "sent": 0, "lost": 0, "active": 0})
                    x["mbit"] += mbit
                    x["rtt"] += got
                    x["sent"] += len(pr)
                    x["lost"] += sum(1 for y in pr if y is None)
                    x["active"] += 1
            for sec, x in sorted(secs.items()):
                agg.append({"relay": label, "relay_size": size, "relay_droplet": relay, "clients": n, "second": sec,
                            "epoch": round(t0 + sec, 3), "clients_active": x["active"], "down_mbit_s": round(x["mbit"], 3),
                            "ping_ms_mean": round(st.mean(x["rtt"]), 2) if x["rtt"] else "", "pings_sent": x["sent"], "pings_lost": x["lost"]})
            proc = r.get("ut_process_cpu_down") or {}
            pv = [v for v in proc.values() if v is not None]
            summ.append({"relay": label, "relay_size": size, "relay_droplet": relay, "clients": n,
                         "agg_mbit": r.get("down_agg_mbit"), "per_client_mean_mbit": r.get("down_avg_mbit"),
                         "per_client_min_mbit": r.get("down_min_mbit"), "complete": n - (r.get("incomplete_down") or 0),
                         "slowest_s": r.get("down_slowest_s"),
                         "ping_median_ms": round(st.median(rtts), 1) if rtts else "", "ping_p95_ms": round(pct(rtts, 95), 1) if rtts else "",
                         "ping_loss_pct": round(100 * lost / (lost + len(rtts)), 1) if lost + len(rtts) else "",
                         "relay_machine_cpu_pct": r.get("ut_machine_cpu_down"), "relay_process_cpu_pct": pv[0] if pv else "",
                         "relay_udp_errors": json.dumps(r.get("ut_udp_errors_down")), "reconnects": r.get("reconnects"),
                         "ping_timeouts_tunnel": r.get("ping_timeouts")})
    for name, data in (("per_second.csv", per), ("per_second_aggregate.csv", agg), ("summary.csv", summ)):
        if data:
            with open(root / name, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(data[0]))
                w.writeheader()
                w.writerows(data)
    ns = sorted({s["clients"] for s in summ})
    labels = [lab for lab, _ in RELAYS if any(s["relay"] == lab for s in summ)]
    get = {(s["relay"], s["clients"]): s for s in summ}
    md = ["Aggregate download, Mbit/s (all clients together, over the window in which all were downloading); in brackets the mean per client", "",
          "| relay | size | " + " | ".join(f"n={n}" for n in ns) + " |", "|---|---|" + "---|" * len(ns)]
    for lab in labels:
        cells = []
        for n in ns:
            s = get.get((lab, n))
            cells.append("" if not s else f"{s['agg_mbit']} ({s['per_client_mean_mbit']})" + ("" if s["complete"] == n else f" {s['complete']}/{n} done"))
        md.append(f"| {lab} | {LABELS[lab]} | " + " | ".join(cells) + " |")
    md += ["", "Ping under load, median / p95 ms (loss %)", "", "| relay | " + " | ".join(f"n={n}" for n in ns) + " |", "|---|" + "---|" * len(ns)]
    for lab in labels:
        md.append(f"| {lab} | " + " | ".join("" if (lab, n) not in get else
                                              f"{get[(lab, n)]['ping_median_ms']} / {get[(lab, n)]['ping_p95_ms']} ({get[(lab, n)]['ping_loss_pct']})" for n in ns) + " |")
    md += ["", "Relay machine CPU % (all cores) / hoprd process % of one core", "", "| relay | " + " | ".join(f"n={n}" for n in ns) + " |", "|---|" + "---|" * len(ns)]
    for lab in labels:
        md.append(f"| {lab} | " + " | ".join("" if (lab, n) not in get else
                                              f"{get[(lab, n)]['relay_machine_cpu_pct']} / {get[(lab, n)]['relay_process_cpu_pct']}" for n in ns) + " |")
    (root / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main(sys.argv[1])
