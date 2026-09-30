"""The ladder T33-relay-baseline, T34-single-relay-scaling and T35-single-exit-scaling share: rungs of n clients
(n = 1, 2, ... from LADDER), each downloading DOWN_BYTES and then uploading UP_BYTES at the same time, each through the
relay and exit the topology assigns (suitelib/relaytopo.py).

Per rung, in order: hold the chain's channel graph against the topology (anything else fails the rung before a byte
moves), connect the rung's clients in parallel, wait IDLE_S, download on every client at once, wait PAUSE_S, upload on
every client at once, disconnect. PAUSE_S also separates the rungs.

Every transfer runs in tests/probes/transferprobe.py inside the client's tools sidecar, which logs cumulative bytes
with epoch timestamps. All transfers of a phase start at one common epoch second (START_LEAD_S after the runner hands
them out, so the time docker-over-ssh takes to reach each client does not stagger them); the spread of the actual
starts is reported (start skew). The rates are those of the overlap: the window from the last transfer's first byte to
the first transfer's last byte, the stretch in which every client was moving data. Aggregate = the bytes all clients
moved inside the window / its length; per client = each client's bytes inside it / its length (mean and minimum). With
one client the window is its whole transfer. A rung passes iff every transfer completes within CAP.

Recorded next to the rates: the node under test (the relay in T33/T34, the exit in T35): its machine's CPU (% of all
its cores) and its hoprd process's CPU (% of one core), over the phase from the common start to the last finish; the
other roles' machines; the hoprd and client versions and one line of machine specs per role; the relays'
forwarded-packet count over the download, which shows the traffic crossed the relays: they forwarded at least one
packet per PKT_BYTES_MAX downloaded bytes (a HOPR packet carries less, so a download that bypassed the relays fails
it), and, where each client's return path is pinned to its relay (paired, not single-exit) with more than one relay,
ATTRIB_MIN_PCT of all relayed packets were on the rung's own relays; reconnects next to tunnel-ping timeouts."""
import json
import statistics as st
import time
from concurrent.futures import ThreadPoolExecutor

from . import relaytopo, shell
from .client import Client, ConnectFailed
from .client import telemetry_sum
from .hosts import Host
from .verdicts import log, utc_now

KNOBS = dict(LADDER="1 2 3 4 5", DOWN_BYTES=25000000, UP_BYTES=25000000, CAP=180, IDLE_S=10, PAUSE_S=10, START_LEAD_S=5,
             RELAY_METRIC='hopr_packets_count{type="forwarded"}', ATTRIB_MIN_PCT=90, PKT_BYTES_MAX=1000)
# T34 and T35 compare one relay and one exit under load: 100 MB each way, 300 s (2.67 Mbit/s) to complete
SCALING_KNOBS = dict(KNOBS, DOWN_BYTES=100000000, UP_BYTES=100000000, CAP=300)
PROBE = "/suite/probes/transferprobe.py"


def timeout(knobs, connect_timeout=240):
    """Worst case per rung: connect, idle, both transfers at their cap, lead times, pauses, log reading; plus slack."""
    rungs = len(str(knobs.LADDER).split())
    per = connect_timeout + int(knobs.IDLE_S) + 2 * (int(knobs.CAP) + int(knobs.START_LEAD_S) + 60) + 2 * int(knobs.PAUSE_S) + 240
    return rungs * per + 300


def cpu_groups(cfg, topo, status):
    """Where to sample CPU: {machine: (Host, role label, {node name: pid})}. One local machine labelled "host" on a
    single-machine stack; on a multi-machine stack (status nodes carry "ssh") every machine that runs a relay, an exit
    or a client, labelled by the roles it plays ("relays", "exits", "clients", or "exits+relays" when shared)."""
    node = {nd["id"]: nd for nd in status.get("nodes", [])}
    opts = cfg.env.get("MULTIHOST_SSH_OPTS", "")
    pids, roles = {}, {}
    for role, ids in (("relays", topo["relays"]), ("exits", topo["exits"])):
        for i in ids:
            nd = node.get(i, {})
            key = nd.get("ssh") or "local"
            pids.setdefault(key, {})[f"node-{i}"] = nd.get("pid")
            roles.setdefault(key, set()).add(role)
    if all(key == "local" for key in pids) and not status.get("multihost"):
        return {"local": (Host(None), "host", pids.get("local", {}))}
    for c in (status.get("clients") or {"local-clients": {"ssh": None}}).values():
        key = c.get("ssh") or "local"
        pids.setdefault(key, {})
        roles.setdefault(key, set()).add("clients")
    return {key: (Host(None if key == "local" else key, opts), "+".join(sorted(roles[key])), pids[key]) for key in pids}


class CpuWindow:
    """Busy % of every machine (100 = all its cores busy), summarised per role label as the mean over the role's
    machines (host_pct) and the busiest one (host_max), and each node's hoprd CPU (% of one core), over a phase."""

    def __init__(self, groups):
        self.groups = groups

    def _sample(self):
        with ThreadPoolExecutor(max(len(self.groups), 1)) as ex:
            return dict(zip(self.groups, ex.map(lambda g: g[0].cpu_sample(g[2].values()), self.groups.values())))

    def __enter__(self):
        self.t0 = time.time()
        self.s0 = self._sample()
        return self

    def __exit__(self, *exc):
        dt = max(time.time() - self.t0, 1e-6)
        s1 = self._sample()
        per_label, self.node_pct, self.machine_pct = {}, {}, {}
        for key, (host, label, pids) in self.groups.items():
            (b0, t0, p0), (b1, t1, p1) = self.s0[key], s1[key]
            pct = round(100 * (b1 - b0) / (t1 - t0), 1) if None not in (b0, t0, b1, t1) and t1 > t0 else None
            self.machine_pct[key] = pct
            if pct is not None:
                per_label.setdefault(label, []).append(pct)
            for name, pid in pids.items():
                a, b = p0.get(pid), p1.get(pid)
                self.node_pct[name] = round(100 * (b - a) / dt, 1) if a is not None and b is not None else None
        self.host_pct = {k: round(sum(v) / len(v), 1) for k, v in per_label.items()}
        self.host_max = {k: max(v) for k, v in per_label.items() if len(v) > 1}
        return False


def _parallel(fn, items):
    with ThreadPoolExecutor(max(len(items), 1)) as ex:
        return list(ex.map(fn, items))


# -- transfers and the overlap ---------------------------------------------------------------------------------------
def interp(samples, t):
    """Cumulative bytes at epoch t from a probe's [[epoch, cumulative], ...] log, linear between samples."""
    if not samples:
        return 0
    if t <= samples[0][0]:
        return samples[0][1] if t == samples[0][0] else 0
    for (t0, b0), (t1, b1) in zip(samples, samples[1:]):
        if t0 <= t <= t1:
            return b0 + (b1 - b0) * ((t - t0) / (t1 - t0) if t1 > t0 else 1)
    return samples[-1][1]


def overlap(results):
    """The stretch in which every transfer was moving: [last first byte, first last byte]. Returns the window, its
    length, every client's Mbit/s inside it, their mean and minimum, the aggregate, and the spread of the starts. A
    transfer that never moved a byte has no window: then every rate is 0."""
    starts = [r["start"] for r in results if r.get("start") is not None]
    skew = round(max(starts) - min(starts), 3) if starts else None
    moving = [r for r in results if r.get("first") is not None and r.get("last") is not None]
    base = {"start_skew_s": skew, "slowest_s": round(max((r["last"] - r["start"]) for r in moving), 2) if moving else None}
    if not results or len(moving) < len(results):
        return dict(base, window=None, window_s=0, per_client_mbit=[0] * len(results), mean_mbit=0, min_mbit=0, agg_mbit=0)
    a, b = max(r["first"] for r in moving), min(r["last"] for r in moving)
    secs = b - a
    if secs <= 0:
        return dict(base, window=[a, b], window_s=0, per_client_mbit=[0] * len(results), mean_mbit=0, min_mbit=0, agg_mbit=0)
    per = [round((interp(r["samples"], b) - interp(r["samples"], a)) * 8 / secs / 1e6, 3) for r in results]
    return dict(base, window=[round(a, 3), round(b, 3)], window_s=round(secs, 2), per_client_mbit=per,
                mean_mbit=round(st.mean(per), 3), min_mbit=min(per), agg_mbit=round(sum(per), 3))


def transfer_phase(group, direction, nbytes, k, target_ip, groups, save):
    """Every client of the rung runs one transfer, all starting at one common epoch second; CPU is sampled from that
    second to the last finish. Returns (probe results, overlap summary, CpuWindow)."""
    start_at = time.time() + float(k.START_LEAD_S)
    cmd = (f"python3 {PROBE} --host {target_ip} --dir {direction} --bytes {int(nbytes)} --start-at {start_at:.3f} "
           f"--timeout {int(k.CAP)}")

    def one(cl):
        raw = cl.out(cmd, timeout=int(k.CAP) + int(k.START_LEAD_S) + 120)
        try:
            return json.loads(raw)
        except ValueError:
            return {"dir": direction, "want": nbytes, "bytes": 0, "complete": False, "samples": [], "start": None,
                    "first": None, "last": None, "code": None, "error": f"no probe output: {raw[-200:]!r}"}

    with ThreadPoolExecutor(max(len(group), 1)) as ex:
        futs = [ex.submit(one, cl) for cl in group]
        wait = start_at - time.time()
        if wait > 0:
            time.sleep(wait)
        with CpuWindow(groups) as cpu:
            res = [f.result() for f in futs]
    for cl, r in zip(group, res):
        save(cl, direction, r)
    return res, overlap(res), cpu


# -- versions and machines -------------------------------------------------------------------------------------------
SPEC_CMD = ("echo \"$(nproc) vCPU, $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | sed 's/^ *//'), "
            "$(free -g | awk '/^Mem/{print $2}') GB RAM, $(curl -s -m 2 http://169.254.169.254/metadata/v1/region || hostname)\"")


def stack_info(cluster, topo, client, groups):
    """hoprd version of a relay and of an exit (REST /node/version), the client's version and image, one line of specs
    per role (identical machines of a role collapse into "N x ...")."""
    hoprd = {}
    for role, ids in (("relay", topo["relays"]), ("exit", topo["exits"])):
        v = cluster.api_json(ids[0], "GET", "/api/v4/node/version", default={}) or {}
        hoprd[role] = v.get("version") or "unknown"
    image = shell.out([*client.docker, "inspect", "-f", "{{.Config.Image}}", client.name], timeout=60) or "unknown"
    with ThreadPoolExecutor(max(len(groups), 1)) as ex:
        specs = list(ex.map(lambda g: (g[1], g[0].out(SPEC_CMD, timeout=60) or "unreachable"), groups.values()))
    by = {}
    for label, line in specs:
        by.setdefault(label, []).append(line)
    machines = {label: "; ".join(f"{lines.count(x)} x {x}" for x in sorted(set(lines))) for label, lines in by.items()}
    return {"hoprd": hoprd, "client": client.version(), "client_image": image, "machines": machines}


def run_ladder(cfg, run, cluster, target, checks, knobs, mode, under_test="relay"):
    """under_test: "relay" (T33, T34) or "exit" (T35): whose machine and process CPU the report leads with."""
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
    groups = cpu_groups(cfg, topo, status)
    info = stack_info(cluster, topo, clients[1], groups)
    checks.row(kind="topology", topology={x: topo.get(x) for x in ("mode", "n", "relays", "exits", "clients", "created", "ready",
                                                                   "exit_channel_funding", "client_image", "hoprd_bin")}, stack=info)
    checks.record(f"stack: hoprd {info['hoprd']['relay']} (relays) / {info['hoprd']['exit']} (exits); client {info['client']} "
                  f"(image {info['client_image']}); machines: " + " | ".join(f"{r}: {m}" for r, m in sorted(info["machines"].items())))
    ut_label = {"relay": "relays", "exit": "exits"}[under_test]
    if list(info["machines"]) == ["host"]:
        ut_label = "host"
    log(f"{mode} topology: relays {topo['relays']}, exits {topo['exits']}; rungs {rungs}; under test: the {under_test}; "
        f"{k.DOWN_BYTES} B down, {k.UP_BYTES} B up, cap {k.CAP}s, idle {k.IDLE_S}s, pause {k.PAUSE_S}s, start lead {k.START_LEAD_S}s")
    tag = checks.test.split("-")[0].lower()

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
        log(f"n={n}: channels ok")
        # (2) connect the rung's clients at once, then the idle
        since = utc_now()

        def connect(c):
            cl = clients[c["k"]]
            try:
                s = cl.connect(c["dest"], 0, ramp_wait_opt_out=True)
                return {"ok": True, "connect_ms": s.connect_ms}
            except ConnectFailed as e:
                return {"ok": False, "error": str(e)}

        # a rung outlasts the 900 s deadman at large n and CAP: idle, two capped transfers with their lead, the pause,
        # and reading every client's log before the disconnect
        for cl in group:
            cl.deadman_cover(int(k.IDLE_S) + 2 * (int(k.CAP) + int(k.START_LEAD_S)) + int(k.PAUSE_S) + 60 * n)
        conn = _parallel(connect, group_t)
        failed = [f"{c['name']} -> {c['dest']}: {r.get('error')}" for c, r in zip(group_t, conn) if not r["ok"]]
        if failed:
            checks.failed(f"n={n}: connect failed: " + "; ".join(failed))
            for cl in group:
                cl.disconnect()
            continue
        time.sleep(k.IDLE_S)
        connected = [cl.is_connected() for cl in group]

        def save(cl, direction, r):
            (run / f"{tag}-n{n}-{direction}-{cl.name}.json").write_text(json.dumps(r))

        # (3) downloads at once, relay packet counts around them; the pause; uploads at once
        before = relay_counts()
        down, dov, down_cpu = transfer_phase(group, "down", k.DOWN_BYTES, k, target.ip, groups, save)
        after = relay_counts()
        up, uov, up_cpu = [], None, None
        if k.UP_BYTES > 0:
            time.sleep(k.PAUSE_S)
            up, uov, up_cpu = transfer_phase(group, "up", k.UP_BYTES, k, target.ip, groups, save)
        errs = [cl.log_errors(since) for cl in group]
        dcmd = [cl.count_log(since, r"received socket command.*command=Disconnect") for cl in group]
        for c, cl in zip(group_t, group):
            cl.save_log(f"{tag}-n{n}-c{c['k']}", since)
        for cl in group:
            cl.disconnect()
        # (4) attribution: the relayed packets during the download went through the relays. In single-exit the exit
        # picks the return relay among all its channels, so every relay counts and no share is scored.
        delta = {r: (after[r] - before[r]) if after.get(r) is not None and before.get(r) is not None else None for r in topo["relays"]}
        mine = sorted(topo["relays"]) if mode == "single-exit" else sorted({c["relay"] for c in group_t})
        tot = sum(v for v in delta.values() if v is not None)
        on_mine = sum(delta[r] or 0 for r in mine)
        attrib_pct = round(100 * on_mine / tot, 1) if tot > 0 else None
        ut_nodes = sorted({c["relay"] for c in group_t}) if under_test == "relay" else sorted({c["exit"] for c in group_t})
        # (5) rows and the rung's numbers
        strip = lambda r: {x: r.get(x) for x in ("want", "bytes", "complete", "code", "error", "start", "first", "last")}  # noqa: E731
        for i, (c, r, e, dc, ok) in enumerate(zip(group_t, down, errs, dcmd, connected)):
            u = up[i] if up else {}
            checks.row(n=n, kind="client", client=c["name"], relay=c["relay"], exit=c["exit"], down=strip(r), up=strip(u) if u else {},
                       down_overlap_mbit=dov["per_client_mbit"][i], up_overlap_mbit=uov["per_client_mbit"][i] if uov else None,
                       connected_before=ok, errors={x: e[x] for x in ("reconnects", "ping_timeouts", "no_surb", "warn_error_lines")},
                       disconnect_cmds=dc)
        rung = {
            "n": n,
            "down_avg_mbit": dov["mean_mbit"], "down_min_mbit": dov["min_mbit"], "down_agg_mbit": dov["agg_mbit"],
            "down_window_s": dov["window_s"], "down_start_skew_s": dov["start_skew_s"], "down_slowest_s": dov["slowest_s"],
            "up_avg_mbit": uov["mean_mbit"] if uov else None, "up_min_mbit": uov["min_mbit"] if uov else None,
            "up_agg_mbit": uov["agg_mbit"] if uov else None, "up_window_s": uov["window_s"] if uov else None,
            "up_start_skew_s": uov["start_skew_s"] if uov else None, "up_slowest_s": uov["slowest_s"] if uov else None,
            "under_test": under_test, "ut_nodes": [f"node-{i}" for i in ut_nodes],
            "ut_machine_cpu_down": down_cpu.host_pct.get(ut_label), "ut_machine_cpu_up": up_cpu.host_pct.get(ut_label) if up_cpu else None,
            "ut_process_cpu_down": {f"node-{i}": down_cpu.node_pct.get(f"node-{i}") for i in ut_nodes},
            "ut_process_cpu_up": {f"node-{i}": up_cpu.node_pct.get(f"node-{i}") for i in ut_nodes} if up_cpu else None,
            "host_cpu_down_pct": down_cpu.host_pct, "host_cpu_up_pct": up_cpu.host_pct if up_cpu else None,
            "host_cpu_down_max": down_cpu.host_max, "host_cpu_up_max": up_cpu.host_max if up_cpu else None,
            "machine_cpu_down_pct": down_cpu.machine_pct,
            "relay_cpu_down_pct": {f"node-{r}": down_cpu.node_pct.get(f"node-{r}") for r in sorted({c["relay"] for c in group_t})},
            "exit_cpu_down_pct": {f"node-{x}": down_cpu.node_pct.get(f"node-{x}") for x in sorted({c["exit"] for c in group_t})},
            "relay_packets_down": {f"node-{r}": v for r, v in delta.items()}, "attrib_pct": attrib_pct,
            "reconnects": sum(e["reconnects"] for e in errs), "ping_timeouts": sum(e["ping_timeouts"] for e in errs),
            "disconnect_cmds": sum(dcmd), "connect_ms": [r.get("connect_ms") for r in conn],
            "incomplete_down": sum(1 for r in down if not r.get("complete")),
            "incomplete_up": sum(1 for u in up if not u.get("complete")),
        }
        table.append(rung)
        checks.row(kind="rung", **rung)
        # (6) verdicts: every transfer within CAP; the traffic crossed the relays
        def why(c, r):
            return f"{c['name']} {r.get('dir')} {r.get('bytes')} B (http {r.get('code')}{', ' + r['error'] if r.get('error') else ''})"
        bad = [why(c, r) for c, r in zip(group_t, down) if not r.get("complete")] + [why(c, u) for c, u in zip(group_t, up) if not u.get("complete")]
        diag = (f"reconnects {rung['reconnects']} (tunnel-ping timeouts {rung['ping_timeouts']}), Disconnect commands {rung['disconnect_cmds']}, "
                f"connected before the transfers {sum(connected)}/{n}, start skew {dov['start_skew_s']}s down / {uov['start_skew_s'] if uov else '-'}s up")
        if bad:
            checks.failed(f"n={n}: {len(bad)} transfer(s) incomplete within CAP={k.CAP}s: " + "; ".join(bad) + f"; {diag}")
        else:
            checks.passed(f"n={n}: all {n} download(s) of {k.DOWN_BYTES} B" + (f" and upload(s) of {k.UP_BYTES} B" if up else "")
                          + f" complete within CAP={k.CAP}s (slowest {max(dov['slowest_s'] or 0, (uov or {}).get('slowest_s') or 0)}s); {diag}")
        if attrib_pct is None:
            checks.warn(f"n={n}: no {k.RELAY_METRIC} counts from the relays; attribution not checked (RELAY_METRIC)")
        else:
            relays_named = ['node-%d' % r for r in mine]
            want = attribution_floor(sum(r.get("bytes") or 0 for r in down), k.PKT_BYTES_MAX)
            checks.assert_min(f"n={n}: packets forwarded by {relays_named if len(relays_named) <= 5 else str(len(relays_named)) + ' relays'} "
                              f"during the download (floor: one per {k.PKT_BYTES_MAX} B downloaded = {want})", on_mine, "packets", "floor", want)
            if len(topo["relays"]) > 1 and mode != "single-exit":
                checks.assert_min(f"n={n}: share of relayed packets on the rung's relay(s) {relays_named} "
                                  f"({on_mine} of {tot})", attrib_pct, "%", "ATTRIB_MIN_PCT", k.ATTRIB_MIN_PCT)
            else:
                checks.record(f"n={n}: share of relayed packets not scored ({'one relay' if len(topo['relays']) == 1 else 'return paths not pinned'}; "
                              f"{on_mine} forwarded)")
        checks.record(f"n={n}: overlap rates, down {rung['down_avg_mbit']} Mbit/s per client (min {rung['down_min_mbit']}), aggregate "
                      f"{rung['down_agg_mbit']} over {rung['down_window_s']}s; up {rung['up_avg_mbit']} per client (min {rung['up_min_mbit']}), "
                      f"aggregate {rung['up_agg_mbit']} over {rung['up_window_s']}s; {under_test} {rung['ut_nodes']}: machine "
                      f"{rung['ut_machine_cpu_down']} % / {rung['ut_machine_cpu_up']} %, process {rung['ut_process_cpu_down']} / "
                      f"{rung['ut_process_cpu_up']} (down / up; process % of one core)")
    if table:
        write_table(run, checks.test, mode, topo, k, table, info, under_test)
    return table


def attribution_floor(nbytes, pkt_bytes_max):
    """Fewest packets the relays must have forwarded for nbytes to have crossed them: a HOPR packet carries at most
    pkt_bytes_max bytes of payload, so fewer means some of the download took another route."""
    return -(-int(nbytes) // int(pkt_bytes_max))


def cpu_text(d, mx=None):
    """{"host": 93.1} -> "93.1 %"; {"clients": 40.0, "relays": 97.2} -> "clients 40.0 % / relays 97.2 %"; with the
    per-role maximum over several machines: "relays 60.1 % (max 71.0)"."""
    if not d:
        return "n/a"
    if list(d) == ["host"]:
        return f"{d['host']} %"
    mx = mx or {}
    return " / ".join(f"{k} {v} %" + (f" (max {mx[k]})" if k in mx else "") for k, v in sorted(d.items()))


def _proc(d):
    vals = [v for v in (d or {}).values() if v is not None]
    return "n/a" if not vals else (f"{vals[0]} %" if len(vals) == 1 else f"{round(sum(vals) / len(vals), 1)} % mean of {len(vals)}")


def write_table(run, test, mode, topo, k, table, info, under_test):
    """<TEST>.md in the run directory: the stack, then one line per rung with the numbers a reader compares."""
    lines = [f"# {test} ({mode}, up to {topo['n']} clients)", "",
             f"- hoprd: {info['hoprd']['relay']} (relays), {info['hoprd']['exit']} (exits)",
             f"- client: {info['client']} (image {info['client_image']})"]
    lines += [f"- {role} machines: {spec}" for role, spec in sorted(info["machines"].items())]
    lines += ["", f"{k.DOWN_BYTES} B down, then {k.UP_BYTES} B up, on every client at once (one common start), cap {k.CAP} s; "
              f"idle {k.IDLE_S} s after connect, {k.PAUSE_S} s between phases and rungs. Rates are over the overlap: from the last "
              f"transfer's first byte to the first transfer's last byte. Under test: the {under_test}; machine CPU is % of all its cores, "
              f"process CPU % of one core, both from the common start to the last finish.", "",
              f"| clients | down per client, mean (min) Mbit/s | down aggregate | up per client, mean (min) | up aggregate "
              f"| {under_test} machine CPU down / up | {under_test} process CPU down / up | start skew down / up s | reconnects (ping timeouts) |",
              "|" + " --- |" * 9]
    for r in table:
        lines.append(f"| {r['n']} | {r['down_avg_mbit']} ({r['down_min_mbit']}) | {r['down_agg_mbit']} | {r['up_avg_mbit']} ({r['up_min_mbit']}) "
                     f"| {r['up_agg_mbit']} | {r['ut_machine_cpu_down']} % / {r['ut_machine_cpu_up']} % "
                     f"| {_proc(r['ut_process_cpu_down'])} / {_proc(r['ut_process_cpu_up'])} | {r['down_start_skew_s']} / {r['up_start_skew_s']} "
                     f"| {r['reconnects']} ({r['ping_timeouts']}) |")
    (run / f"{test}.md").write_text("\n".join(lines) + "\n")
