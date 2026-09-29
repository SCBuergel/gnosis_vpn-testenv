"""The ladder T33-relay-baseline and T34-single-relay-scaling share: rungs of n clients (n = 1, 2, ... from LADDER),
each downloading DOWN_BYTES and then uploading UP_BYTES at the same time, each through its own exit and the relay
the topology assigns (suitelib/relaytopo.py).

Per rung, in order: hold the chain's channel graph against the topology (every client and every exit exactly one
Open channel, to its relay; anything else fails the rung before a byte moves), connect the rung's clients in
parallel, wait IDLE_S, download in parallel, wait PAUSE_S, upload in parallel, disconnect. PAUSE_S also separates
the rungs. Each transfer runs under curl's CAP; a rung passes iff every transfer of it completes within CAP.

Recorded next to the rates: host CPU (all cores, /proc/stat) and each relay's and exit's hoprd CPU over each phase,
because on one host the machine is the likely ceiling and the baseline is only readable next to it; each relay's
forwarded-packet count over the download, which shows the traffic crossed the assigned relays: they forwarded at
least one packet per PKT_BYTES_MAX downloaded bytes (a HOPR packet carries less, so a download that bypassed the relays
fails it), and, with more than one relay in the topology, ATTRIB_MIN_PCT of all relayed packets were on the rung's own
relays (with one relay that share is 100 % by construction and is not scored); reconnects next to tunnel-ping timeouts."""
import os
import statistics as st
import time
from concurrent.futures import ThreadPoolExecutor

from . import relaytopo
from .client import Client, ConnectFailed
from .client import telemetry_sum
from .target import curl_down, curl_up
from .verdicts import log, utc_now

KNOBS = dict(LADDER="1 2 3 4 5", DOWN_BYTES=25000000, UP_BYTES=25000000, CAP=180, IDLE_S=10, PAUSE_S=10,
             RELAY_METRIC='hopr_packets_count{type="forwarded"}', ATTRIB_MIN_PCT=90, PKT_BYTES_MAX=1000)


def timeout(knobs, connect_timeout=240):
    """Worst case per rung: connect, idle, both transfers at their cap, pauses, log reading; plus slack."""
    rungs = len(str(knobs.LADDER).split())
    per = connect_timeout + int(knobs.IDLE_S) + 2 * (int(knobs.CAP) + 30) + 2 * int(knobs.PAUSE_S) + 180
    return rungs * per + 300


_TICK = os.sysconf("SC_CLK_TCK")


def host_cpu():
    """(busy, total) jiffies over all cores from /proc/stat."""
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v) - idle, sum(v)


def pid_cpu(pid):
    """utime + stime of a process in seconds, or None when it is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            p = f.read().rsplit(")", 1)[1].split()
        return (int(p[11]) + int(p[12])) / _TICK
    except (OSError, IndexError, ValueError):
        return None


class CpuWindow:
    """Host busy % (100 = every core busy) and per-node hoprd CPU (% of one core) over a phase."""

    def __init__(self, pids):
        self.pids = pids

    def __enter__(self):
        self.t0 = time.time()
        self.h0 = host_cpu()
        self.p0 = {k: pid_cpu(p) for k, p in self.pids.items()}
        return self

    def __exit__(self, *exc):
        dt = max(time.time() - self.t0, 1e-6)
        h1 = host_cpu()
        busy, total = h1[0] - self.h0[0], h1[1] - self.h0[1]
        self.host_pct = round(100 * busy / total, 1) if total > 0 else None
        self.node_pct = {}
        for k, p in self.pids.items():
            a, b = self.p0.get(k), pid_cpu(p)
            self.node_pct[k] = round(100 * (b - a) / dt, 1) if a is not None and b is not None else None
        return False


def _parallel(fn, items):
    with ThreadPoolExecutor(max(len(items), 1)) as ex:
        return list(ex.map(fn, items))


def _phase(group, fn, pids):
    """Run fn(client) for every client at once; returns (results, wall seconds, CpuWindow)."""
    with CpuWindow(pids) as cpu:
        t0 = time.time()
        res = _parallel(fn, group)
        wall = time.time() - t0
    return res, round(wall, 2), cpu


def attribution_floor(nbytes, pkt_bytes_max):
    """Fewest packets the relays must have forwarded for nbytes to have crossed them: a HOPR packet carries at most
    pkt_bytes_max bytes of payload, so fewer means some of the download took another route."""
    return -(-int(nbytes) // int(pkt_bytes_max))


def _mbit(nbytes, secs):
    return round(nbytes * 8 / secs / 1e6, 3) if secs and secs > 0 else 0


def run_ladder(cfg, run, cluster, target, checks, knobs, mode):
    k = knobs
    topo = relaytopo.load(cfg.config_dir)
    if topo is None or topo.get("mode") != mode:
        have = f"a '{topo.get('mode')}' topology" if topo else "no relay topology"
        checks.skip(f"needs the '{mode}' relay topology (just relay-topology {mode} N); CONFIG_DIR holds {have}")
    stale = relaytopo.stale_reason(topo, cluster.status())
    if stale:
        checks.skip(f"the saved '{mode}' topology is not the running stack ({stale}); just relay-topology {mode} N")
    rungs = k.numbers("LADDER", lo=1, ints=True)
    too_big = [n for n in rungs if n > topo["n"]]
    if too_big:
        checks.failed(f"LADDER rungs {too_big} exceed the topology's {topo['n']} clients (just relay-topology {mode} {max(too_big)})")
        rungs = [n for n in rungs if n <= topo["n"]]
    tclients = topo["clients"]
    clients = {c["k"]: Client(cfg, run, name=c["name"], index=c["k"]) for c in tclients}
    missing = [c["name"] for c in tclients if not clients[c["k"]].exists() or not clients[c["k"]].tools_exists()]
    if missing:
        checks.failed(f"client container or tools sidecar missing: {missing} (just relay-topology {mode} {topo['n']})")
        return
    status = cluster.status() or {}
    node = {nd["id"]: nd for nd in status.get("nodes", [])}
    pids = {f"node-{i}": node.get(i, {}).get("pid") for i in topo["relays"] + topo["exits"]}
    checks.row(kind="topology", topology={x: topo.get(x) for x in ("mode", "n", "relays", "exits", "clients", "created", "ready",
                                                                   "exit_channel_funding", "client_image", "hoprd_bin")},
               client_version=clients[1].version())
    log(f"{mode} topology: relays {topo['relays']}, exits {topo['exits']}; rungs {rungs}; "
        f"{k.DOWN_BYTES} B down, {k.UP_BYTES} B up, cap {k.CAP}s, idle {k.IDLE_S}s, pause {k.PAUSE_S}s")

    def relay_counts():
        return {r: telemetry_sum(cluster.metrics(r), k.RELAY_METRIC) for r in topo["relays"]}

    table = []
    for idx, n in enumerate(rungs):
        if idx:
            time.sleep(k.PAUSE_S)                     # before every subsequent rung
        group_t = tclients[:n]
        group = [clients[c["k"]] for c in group_t]
        # (1) the channel graph is the topology, before anything connects
        chans = cluster.api_json(topo["relays"][0], "GET", "/api/v4/channels?fullTopology=true", default={}) or {}
        problems, summary = relaytopo.check_channels(topo, chans.get("all", []))
        checks.row(n=n, kind="channels", problems=problems, channels_out=summary)
        if problems:
            checks.failed(f"n={n}: channel graph is not the {mode} topology, rung not run: " + "; ".join(problems))
            continue
        log(f"n={n}: channels ok ({len(tclients)} clients, {len(set(c['exit'] for c in tclients))} exits, one channel each to its relay)")
        # (2) connect the rung's clients at once, then the idle
        since = utc_now()

        def connect(c):
            cl = clients[c["k"]]
            try:
                s = cl.connect(c["dest"], 0, ramp_wait_opt_out=True)
                return {"ok": True, "connect_ms": s.connect_ms}
            except ConnectFailed as e:
                return {"ok": False, "error": str(e)}

        conn = _parallel(connect, group_t)
        failed = [f"{c['name']} -> {c['dest']}: {r.get('error')}" for c, r in zip(group_t, conn) if not r["ok"]]
        if failed:
            checks.failed(f"n={n}: connect failed: " + "; ".join(failed))
            for cl in group:
                cl.disconnect()
            continue
        time.sleep(k.IDLE_S)
        connected = [cl.is_connected() for cl in group]
        # (3) downloads at once, relay packet counts around them
        before = relay_counts()
        down, down_wall, down_cpu = _phase(group, lambda cl: curl_down(cl, target.ip, k.DOWN_BYTES, k.CAP), pids)
        after = relay_counts()
        up, up_wall, up_cpu = [], 0, None
        if k.UP_BYTES > 0:
            time.sleep(k.PAUSE_S)
            up, up_wall, up_cpu = _phase(group, lambda cl: curl_up(cl, target.ip, k.UP_BYTES, k.CAP), pids)
        errs = [cl.log_errors(since) for cl in group]
        dcmd = [cl.count_log(since, r"received socket command.*command=Disconnect") for cl in group]
        for c, cl in zip(group_t, group):
            cl.save_log(f"{checks.test.split('-')[0].lower()}-n{n}-c{c['k']}", since)
        for cl in group:
            cl.disconnect()
        # (4) attribution: the relayed packets during the download went through the rung's relays
        delta = {r: (after[r] - before[r]) if after.get(r) is not None and before.get(r) is not None else None for r in topo["relays"]}
        mine = sorted({c["relay"] for c in group_t})
        known = [v for v in delta.values() if v is not None]
        tot = sum(known)
        on_mine = sum(delta[r] or 0 for r in mine)
        attrib_pct = round(100 * on_mine / tot, 1) if tot > 0 else None
        # (5) rows and the rung's numbers
        for c, r, u, e, dc, ok in zip(group_t, down, up or [{}] * n, errs, dcmd, connected):
            checks.row(n=n, kind="client", client=c["name"], relay=c["relay"], exit=c["exit"], down=r, up=u, connected_before=ok,
                       errors={x: e[x] for x in ("reconnects", "ping_timeouts", "no_surb", "warn_error_lines")}, disconnect_cmds=dc)
        rung = {
            "n": n,
            "down_avg_mbit": round(st.mean([r["mbit"] for r in down]), 3),
            "down_min_mbit": min(r["mbit"] for r in down),
            "down_agg_mbit": _mbit(sum(r["bytes"] for r in down), down_wall),
            "down_wall_s": down_wall,
            "up_avg_mbit": round(st.mean([u["mbit"] for u in up]), 3) if up else None,
            "up_min_mbit": min(u["mbit"] for u in up) if up else None,
            "up_agg_mbit": _mbit(sum(u["bytes"] for u in up), up_wall) if up else None,
            "up_wall_s": up_wall or None,
            "host_cpu_down_pct": down_cpu.host_pct, "host_cpu_up_pct": up_cpu.host_pct if up_cpu else None,
            "relay_cpu_down_pct": {f"node-{r}": down_cpu.node_pct.get(f"node-{r}") for r in mine},
            "relay_cpu_up_pct": {f"node-{r}": up_cpu.node_pct.get(f"node-{r}") for r in mine} if up_cpu else None,
            "exit_cpu_down_pct": {f"node-{c['exit']}": down_cpu.node_pct.get(f"node-{c['exit']}") for c in group_t},
            "relay_packets_down": {f"node-{r}": v for r, v in delta.items()},
            "attrib_pct": attrib_pct,
            "reconnects": sum(e["reconnects"] for e in errs), "ping_timeouts": sum(e["ping_timeouts"] for e in errs),
            "disconnect_cmds": sum(dcmd), "connect_ms": [r.get("connect_ms") for r in conn],
            "incomplete_down": sum(1 for r in down if not r["complete"]),
            "incomplete_up": sum(1 for u in up if not u["complete"]),
        }
        table.append(rung)
        checks.row(kind="rung", **rung)
        # (6) verdicts: every transfer within CAP; the traffic crossed the rung's relay(s)
        bad = [f"{c['name']} down {r['bytes']} B in {r['elapsed']}s (http {r['code']})" for c, r in zip(group_t, down) if not r["complete"]]
        bad += [f"{c['name']} up {u['bytes']} B in {u['elapsed']}s (http {u['code']})" for c, u in zip(group_t, up) if not u["complete"]]
        diag = (f"reconnects {rung['reconnects']} (tunnel-ping timeouts {rung['ping_timeouts']}), Disconnect commands {rung['disconnect_cmds']}, "
                f"connected before the transfers {sum(connected)}/{n}")
        if bad:
            checks.failed(f"n={n}: {len(bad)} transfer(s) incomplete within CAP={k.CAP}s: " + "; ".join(bad) + f"; {diag}")
        else:
            checks.passed(f"n={n}: all {n} download(s) of {k.DOWN_BYTES} B" + (f" and upload(s) of {k.UP_BYTES} B" if up else "")
                          + f" complete within CAP={k.CAP}s (slowest {max(r['elapsed'] for r in down + up)}s); {diag}")
        if attrib_pct is None:
            checks.warn(f"n={n}: no {k.RELAY_METRIC} counts from the relays; attribution not checked (RELAY_METRIC)")
        else:
            relays_named = ['node-%d' % r for r in mine]
            want = attribution_floor(sum(r["bytes"] for r in down), k.PKT_BYTES_MAX)
            checks.assert_min(f"n={n}: packets forwarded by {relays_named} during the download (floor: one per "
                              f"{k.PKT_BYTES_MAX} B downloaded = {want})", on_mine, "packets", "floor", want)
            if len(topo["relays"]) > 1:
                checks.assert_min(f"n={n}: share of relayed packets on the rung's relay(s) {relays_named} "
                                  f"({on_mine} of {tot})", attrib_pct, "%", "ATTRIB_MIN_PCT", k.ATTRIB_MIN_PCT)
            else:
                checks.record(f"n={n}: one relay in the topology, share of relayed packets not scored ({on_mine} forwarded)")
        checks.record(f"n={n}: down {rung['down_avg_mbit']} Mbit/s per client (min {rung['down_min_mbit']}), "
                      f"aggregate {rung['down_agg_mbit']}; up {rung['up_avg_mbit']} per client, aggregate {rung['up_agg_mbit']}; "
                      f"host CPU {rung['host_cpu_down_pct']} % down / {rung['host_cpu_up_pct']} % up, "
                      f"relay CPU (one core = 100) {rung['relay_cpu_down_pct']}")
    if table:
        write_table(run, checks.test, mode, topo, k, table)
    return table


def write_table(run, test, mode, topo, k, table):
    """<TEST>.md in the run directory: one line per rung, the numbers a reader compares."""
    head = ("| clients | down Mbit/s per client (min) | down aggregate | up Mbit/s per client (min) | up aggregate "
            "| host CPU down / up % | relay CPU down % | reconnects (ping timeouts) |")
    lines = [f"# {test} ({mode}, {topo['n']} clients max)", "",
             f"{k.DOWN_BYTES} B down then {k.UP_BYTES} B up per client, all clients at once, cap {k.CAP} s, "
             f"idle {k.IDLE_S} s after connect, {k.PAUSE_S} s between phases and rungs. Relay CPU is % of one core.", "",
             head, "|" + " --- |" * 8]
    for r in table:
        lines.append(f"| {r['n']} | {r['down_avg_mbit']} ({r['down_min_mbit']}) | {r['down_agg_mbit']} | {r['up_avg_mbit']} ({r['up_min_mbit']}) "
                     f"| {r['up_agg_mbit']} | {r['host_cpu_down_pct']} / {r['host_cpu_up_pct']} "
                     f"| {', '.join(f'{a} {b}' for a, b in r['relay_cpu_down_pct'].items())} | {r['reconnects']} ({r['ping_timeouts']}) |")
    (run / f"{test}.md").write_text("\n".join(lines) + "\n")
