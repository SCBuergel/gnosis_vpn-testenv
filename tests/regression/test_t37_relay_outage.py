"""T37-relay-outage (diagnostic): what a relay that vanishes mid-transfer does to a running tunnel. One client whose
traffic spreads over N relays (the `multi-relay` topology: the client and its exit each hold a channel to every relay,
so both directions plan their 1-hop paths across all N) downloads without pause, and one relay after another is taken
out, STEP_S apart, until one is left: N phases of STEP_S=60 s with N, N-1, ..., 1 relays alive.

Shape: connect, IDLE_S=10, then one long download (DOWN_BYTES=10000000000, never meant to complete) that pings the
target through the tunnel once a second for its length; a download that ends early (the tunnel or the connection
broke) is started again at once, so the byte log and the pings cover the whole run, and every restart is recorded.
At the start of phase i (i = 2..N) the relay ORDER[i-2] is taken out (ORDER="" : node-0, node-1, ...):

- OUTAGE=drop (default): every packet to and from the relay's machine is dropped (iptables, ssh excepted), the
  machine is gone from the network without a word: no reset, no close, the way a power cut or a lost uplink looks
  to its peers. Undone after the test, so the stack stays usable.
- OUTAGE=kill: the relay's hoprd gets SIGKILL; its machine's kernel still answers (a refused connection). Not undone:
  the topology needs a fresh `multihost-up` after it.

Recorded (never scored): per phase the download rate (mean over the phase, and the 1-s bins' min / median), the
seconds in which no byte arrived, the longest stall, the median and 95th percentile ping and the share of echoes lost;
the time from each outage to the first second back at half the previous phase's mean (recovery); per relay its share
of the forwarded packets per phase (RELAY_METRIC from each relay's /metrics, every RELAY_SAMPLE_S=5 s); the client's
reconnects and tunnel ping timeouts. Files in the run: t37-seconds.csv (every second: Mbit/s, relays alive, ping),
t37-transfers.json (the raw probe results), t37-relays.json (the relay counters), T37-relay-outage.md.

Setup: `just multihost-up HOSTS multi-relay 5` (each relay on a machine of its own for OUTAGE=drop), then
`just multihost-test t37`; on AWS `multihost/relay_outage.py run`."""
import csv
import json
import statistics as st
import threading
import time

from suitelib import relaybench, relaytopo
from suitelib.client import Client, ConnectFailed
from suitelib.hosts import Host
from suitelib.stats import ping_summary
from suitelib.verdicts import log, utc_now

TEST = "T37-relay-outage"
KIND = "diagnostic"
GROUP = "resilience"
KNOBS = dict(STEP_S=60, DOWN_BYTES=10000000000, OUTAGE="drop", ORDER="", IDLE_S=10, RELAY_SAMPLE_S=5,
             RELAY_METRIC='hopr_packets_count{type="forwarded"}')
CHAIN = "GVPN_OUTAGE"
DROP = (f"iptables -N {CHAIN} 2>/dev/null; iptables -F {CHAIN}; "
        f"iptables -A {CHAIN} -p tcp --dport 22 -j RETURN; iptables -A {CHAIN} -p tcp --sport 22 -j RETURN; "
        f"iptables -A {CHAIN} -s 127.0.0.0/8 -j RETURN; iptables -A {CHAIN} -d 127.0.0.0/8 -j RETURN; "
        f"iptables -A {CHAIN} -j DROP; iptables -I INPUT 1 -j {CHAIN}; iptables -I OUTPUT 1 -j {CHAIN}; echo dropped")
UNDROP = (f"iptables -D INPUT -j {CHAIN}; iptables -D OUTPUT -j {CHAIN}; iptables -F {CHAIN}; iptables -X {CHAIN}; "
          "echo restored")


def TIMEOUT(knobs):
    # the relay count is not a knob (it is the topology's): room for up to 32 relays
    return 900 + 32 * int(knobs.STEP_S)


def relay_host(cluster, r):
    st_ = cluster.status() or {}
    nd = cluster.node(r)
    return Host(nd.get("ssh"), st_.get("ssh_options", "")), nd


def take_out(cluster, r, how):
    h, nd = relay_host(cluster, r)
    if how == "kill":
        pid = int(nd.get("pid") or 0)
        return h.out(f"kill -9 {pid} && echo killed {pid}", timeout=30) if pid else "no pid"
    return h.out(DROP, timeout=30)


def bring_back(cluster, r):
    h, _ = relay_host(cluster, r)
    return h.out(UNDROP, timeout=30)


def seconds(results, t0, t1):
    """[(second, Mbit/s)] for every whole second of [t0, t1), summed over every probe result (restarts included)."""
    out = []
    for i in range(int(t1 - t0)):
        a, b = t0 + i, t0 + i + 1
        got = sum(relaybench.interp(r["samples"], b) - relaybench.interp(r["samples"], a) for r in results if r.get("samples"))
        out.append(round(got * 8 / 1e6, 3))
    return out


def test_relay_outage(cfg, run, cluster, target, checks, knobs):
    k = knobs
    topo = relaytopo.load(cfg.config_dir)
    if topo is None or topo.get("mode") != "multi-relay":
        checks.skip("needs the 'multi-relay' topology (just multihost-up HOSTS multi-relay N); CONFIG_DIR holds "
                    + (f"a '{topo.get('mode')}' topology" if topo else "no relay topology"))
    stale = relaytopo.stale_reason(topo, cluster.status())
    if stale:
        checks.skip(f"the saved multi-relay topology is not the running stack ({stale})")
    relays = list(topo["relays"])
    order = [int(x) for x in str(k.ORDER).split()] if str(k.ORDER).strip() else relays[:-1]
    if len(relays) < 2 or len(order) != len(relays) - 1 or not set(order) <= set(relays) or len(set(order)) != len(order):
        checks.failed(f"ORDER {order}: want {len(relays) - 1} distinct relays of {relays}")
        return
    how = str(k.OUTAGE)
    if how not in ("drop", "kill"):
        checks.failed(f"OUTAGE={how}: drop or kill")
        return
    machines = [cluster.node(r).get("machine") for r in relays]
    if how == "drop" and (None in machines or len(set(machines)) != len(machines)):
        checks.skip(f"OUTAGE=drop takes a relay's machine off the network: every relay needs a machine of its own ({machines})")
    c = topo["clients"][0]
    cl = Client(cfg, run, name=c["name"], index=c["k"])
    if not cl.exists() or not cl.tools_exists():
        checks.failed(f"client {c['name']} or its tools sidecar missing")
        return
    chans = cluster.api_json(relays[0], "GET", "/api/v4/channels?fullTopology=true", default={}) or {}
    problems, summary = relaytopo.check_channels(topo, chans.get("all", []))
    checks.row(kind="channels", problems=problems, channels_out=summary)
    if problems:
        checks.failed("channel graph is not the multi-relay topology: " + "; ".join(problems))
        return
    step, n = int(k.STEP_S), len(relays)
    total = step * n
    checks.record(f"{n} relays {', '.join(f'node-{r}' for r in relays)}; outage {how}, one every {step} s in the order "
                  f"{', '.join(f'node-{r}' for r in order)}; {total} s of download")

    since = utc_now()
    cl.deadman_cover(total + int(k.IDLE_S) + 600)
    try:
        s = cl.connect(c["dest"], 0, ramp_wait_opt_out=True)
    except ConnectFailed as e:
        checks.failed(f"connect failed: {e}")
        return
    checks.record(f"connected in {s.connect_ms} ms; idle {k.IDLE_S} s")
    time.sleep(int(k.IDLE_S))

    t0 = time.time() + 3
    t_end = t0 + total
    results, restarts = [], []
    stop = threading.Event()

    def download():
        first = True
        while not stop.is_set():
            left = t_end - time.time()
            if left < 2:
                return
            start_at = t0 if first else 0
            first = False
            iface = cl.out('ls /sys/class/net | grep -m1 "^wg" || true').strip()
            cmd = (f"python3 {relaybench.PROBE} --host {target.ip} --dir down --bytes {int(k.DOWN_BYTES)} --start-at {start_at:.3f} "
                   f"--timeout {int(left) + (3 if start_at else 0)} --ping {target.ip}" + (f" --iface {iface}" if iface else ""))
            raw = cl.out(cmd, timeout=int(left) + 120)
            try:
                r = json.loads(raw)
            except ValueError:
                r = {"bytes": 0, "samples": [], "pings": [], "error": f"no probe output: {raw[-200:]!r}", "start": time.time()}
            r["iface"] = iface
            results.append(r)
            if time.time() < t_end - 2:
                restarts.append({"at": round(time.time() - t0, 1), "error": r.get("error"), "bytes": r.get("bytes"), "iface": iface})
                log(f"download ended at +{time.time() - t0:.0f} s ({r.get('error')}); starting another")
                time.sleep(1)

    relay_rows = []

    def sample_relays():
        while not stop.is_set() and time.time() < t_end + 10:
            row = {"t": round(time.time() - t0, 2)}

            def one(r):
                txt = cluster.api(r, "GET", "/metrics", timeout=3)
                return relaybench.telemetry_sum(txt, k.RELAY_METRIC) if txt else None

            row.update({f"node-{r}": v for r, v in zip(relays, relaybench._parallel(one, relays))})
            relay_rows.append(row)
            time.sleep(float(k.RELAY_SAMPLE_S))

    dl = threading.Thread(target=download, daemon=True)
    sm = threading.Thread(target=sample_relays, daemon=True)
    dl.start()
    sm.start()
    outages, removed = [], []
    try:
        for i, r in enumerate(order, start=1):
            time.sleep(max(0.0, t0 + i * step - time.time()))
            at = round(time.time() - t0, 2)
            res = take_out(cluster, r, how)
            removed.append(r)
            outages.append({"relay": r, "at": at, "how": how, "result": res.strip()[-120:]})
            log(f"+{at:.0f} s: node-{r} taken out ({how}): {res.strip()[-80:]}; {n - i} relay(s) left")
        dl.join(timeout=total + 300)
    finally:
        stop.set()
        if how == "drop":
            for r in removed:
                log(f"node-{r}: {bring_back(cluster, r).strip()}")
    sm.join(timeout=30)
    errs = cl.log_errors(since)
    cl.save_log("t37-client", since)
    cl.disconnect()

    # per second, per phase
    sec = seconds(results, t0, t_end)
    pings = [p for r in results for p in (r.get("pings") or [])]
    ping_at = {}
    for p in pings:
        ping_at.setdefault(int(p[1] - t0), []).append(p[2])
    with open(run / "t37-seconds.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["second", "relays_alive", "mbit", "ping_ms", "ping_lost"])
        for i, v in enumerate(sec):
            ps = ping_at.get(i, [])
            ok = [x for x in ps if x is not None]
            w.writerow([i, n - min(i // step, n - 1), v, round(st.median(ok), 1) if ok else "", len(ps) - len(ok)])
    (run / "t37-transfers.json").write_text(json.dumps({"t0": t0, "results": results, "restarts": restarts, "outages": outages}))
    (run / "t37-relays.json").write_text(json.dumps(relay_rows))

    phases = []
    for i in range(n):
        a, b = i * step, (i + 1) * step
        bins = sec[a:b]
        ph_pings = [p for p in pings if a <= p[1] - t0 < b]
        ps = ping_summary([{"pings": ph_pings}]) or {}
        zero = sum(1 for v in bins if v == 0)
        longest, cur = 0, 0
        for v in bins:
            cur = cur + 1 if v == 0 else 0
            longest = max(longest, cur)
        share = {}
        before = [row for row in relay_rows if row["t"] <= a + 1]
        after = [row for row in relay_rows if row["t"] <= b + 1]
        if before and after:
            d = {f"node-{r}": (after[-1].get(f"node-{r}") or 0) - (before[-1].get(f"node-{r}") or 0)
                 for r in relays if after[-1].get(f"node-{r}") is not None and before[-1].get(f"node-{r}") is not None}
            tot = sum(v for v in d.values() if v > 0)
            share = {kk: round(100 * v / tot, 1) for kk, v in d.items()} if tot else {}
        phases.append({"phase": i + 1, "relays_alive": n - i, "removed_at_start": order[i - 1] if i else None,
                       "mbit_mean": round(st.mean(bins), 2) if bins else None, "mbit_min": min(bins) if bins else None,
                       "mbit_median": round(st.median(bins), 2) if bins else None, "zero_seconds": zero, "longest_stall_s": longest,
                       "ping_p50_ms": ps.get("p50_ms"), "ping_p95_ms": ps.get("p95_ms"), "ping_loss_pct": ps.get("loss_pct"),
                       "ping_sent": ps.get("sent"), "forwarded_share_pct": share})
    for i in range(1, n):
        prev = phases[i - 1]["mbit_mean"] or 0
        bins = sec[i * step:(i + 1) * step]
        back = next((j for j, v in enumerate(bins) if v >= prev / 2), None) if prev else None
        phases[i]["recovery_s"] = back
    for ph in phases:
        checks.row(kind="phase", **ph)
        checks.record(f"phase {ph['phase']} ({ph['relays_alive']} relays" + (f", node-{ph['removed_at_start']} out at its start" if ph["removed_at_start"] is not None else "")
                      + f"): {ph['mbit_mean']} Mbit/s (1-s min {ph['mbit_min']}, median {ph['mbit_median']}), {ph['zero_seconds']} s without a byte "
                      f"(longest {ph['longest_stall_s']} s), ping p50 {ph['ping_p50_ms']} ms p95 {ph['ping_p95_ms']} ms, loss {ph['ping_loss_pct']} %"
                      + (f", back at half the previous rate after {ph.get('recovery_s')} s" if ph["removed_at_start"] is not None else "")
                      + (f"; forwarded share {ph['forwarded_share_pct']}" if ph["forwarded_share_pct"] else ""))
    checks.row(kind="run", outages=outages, restarts=restarts, client_errors=errs)
    checks.record(f"download restarts: {len(restarts)}" + (f" ({'; '.join(f'+{x['at']} s: {x['error']}' for x in restarts)})" if restarts else "")
                  + f"; client: {errs.get('reconnects', 0)} reconnects, {errs.get('ping_timeouts', 0)} tunnel ping timeouts")

    lines = [f"# {TEST}", "", f"- {n} relays, outage `{how}`, one every {step} s: " + ", ".join(f"node-{o['relay']} at +{o['at']:.0f} s" for o in outages),
             f"- download restarts: {len(restarts)}; client reconnects {errs.get('reconnects', 0)}, tunnel ping timeouts {errs.get('ping_timeouts', 0)}", "",
             "| phase | relays | Mbit/s mean | 1-s min / median | s without bytes | longest stall s | recovery s | ping p50 / p95 ms | ping loss % |",
             "|---|---|---|---|---|---|---|---|---|"]
    for ph in phases:
        lines.append(f"| {ph['phase']} | {ph['relays_alive']} | {ph['mbit_mean']} | {ph['mbit_min']} / {ph['mbit_median']} | {ph['zero_seconds']} "
                     f"| {ph['longest_stall_s']} | {ph.get('recovery_s', '')} | {ph['ping_p50_ms']} / {ph['ping_p95_ms']} | {ph['ping_loss_pct']} |")
    (run / f"{TEST}.md").write_text("\n".join(lines) + "\n")
