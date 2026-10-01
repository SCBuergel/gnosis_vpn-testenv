"""The ladder T33-relay-baseline, T34-single-relay-scaling and T35-single-exit-scaling share: rungs of n clients
(n = 1, 2, ... from LADDER), each downloading DOWN_BYTES and then uploading UP_BYTES at the same time, each through the
relay and exit the topology assigns (suitelib/relaytopo.py).

Per rung, in order: hold the chain's channel graph against the topology (anything else fails the rung before a byte
moves), connect the rung's clients in parallel, wait IDLE_S, download on every client at once, wait PAUSE_S, upload on
every client at once, disconnect. PAUSE_S also separates the rungs.

Every transfer runs in tests/probes/transferprobe.py inside the client's tools sidecar, bound to the tunnel interface,
and logs cumulative bytes with epoch timestamps: bytes read for a download, bytes the target's TCP acknowledged for an
upload (not bytes handed to the socket: the send buffer made the first version's upload rates too high). All transfers
of a phase start at one common epoch second (START_LEAD_S after the runner hands them out, so the time
docker-over-ssh takes to reach each client does not stagger them). "Start skew" is the spread of the probes' own start
stamps: it shows that no probe started late, NOT that the machines' clocks agree (every probe sleeps until the same
reading of its own clock); the clocks are held against the runner's once per run and reported (`clock` in the topology
row). The rates are those of the overlap: the window from the last transfer's first byte to the first transfer's last
byte, the stretch in which every client was moving data. Aggregate = the bytes all clients moved inside the window /
its length; per client = each client's bytes inside it / its length (mean and minimum). With one client the window is
its whole transfer. A mean hides a path that collapses and recovers, so the aggregate is also cut into BUCKET_S-second
buckets (minimum, median, maximum) and each transfer's longest stretch without progress is reported.

A rung passes iff every transfer completes within CAP and the evidence that it took the intended path holds:

- per client: the tunnel interface's own byte counters grew by at least the transfer's bytes (rx for a download, tx
  for an upload), so no transfer went around the tunnel;
- the relays forwarded at least one packet per PKT_BYTES_MAX transferred bytes, during the download AND during the
  upload (a HOPR packet carries less, so a transfer that bypassed the relays fails it), and, where each client's
  return path is pinned to its relay (paired, not single-exit) with more than one relay, ATTRIB_MIN_PCT of all
  relayed packets were on the rung's own relays;
- where the node under test has machines of its own (a multi-machine stack): those machines' wire interfaces carried
  at least the transferred bytes in the direction of the transfer (out of an exit and through a relay for a download,
  into them for an upload). The wire carries HOPR packets, so it must exceed the payload.

Recorded next to the rates: the node under test (the relay in T33/T34, the exit in T35): its machine's CPU (% of all
its cores) and its hoprd process's CPU (% of one core), both over the same overlap window as the rates (sampled every
SAMPLE_S seconds on the machine itself, stamped with its clock; the first version averaged CPU from the common start
to the last finish, a longer stretch than the one the rates cover, which understated the CPU per Mbit/s); its
machine's UDP error counters (RcvbufErrors and friends: a socket buffer that overflows drops packets while the CPU
still looks idle); its hoprd's own packet counters (received, sent, rejected, dropped at the egress ring buffer); the
other roles' machines; the frames each client discarded (return-path loss as the client sees it); the hoprd and
client versions and one line of machine specs per role; reconnects next to tunnel-ping timeouts."""
import json
import statistics as st
import time
from concurrent.futures import ThreadPoolExecutor

from . import relaytopo, shell
from .client import Client, ConnectFailed
from .client import telemetry_sum
from .hosts import UDP_ERRORS, Host, Sampler, machine_window
from .verdicts import log, utc_now

KNOBS = dict(LADDER="1 2 3 4 5", DOWN_BYTES=25000000, UP_BYTES=25000000, CAP=180, IDLE_S=10, PAUSE_S=10, START_LEAD_S=5,
             RELAY_METRIC='hopr_packets_count{type="forwarded"}', ATTRIB_MIN_PCT=90, PKT_BYTES_MAX=1000, BUCKET_S=5, SAMPLE_S=2)
# T34 and T35 compare one relay and one exit under load: 100 MB each way, 300 s (2.67 Mbit/s) to complete
SCALING_KNOBS = dict(KNOBS, DOWN_BYTES=100000000, UP_BYTES=100000000, CAP=300)
PROBE = "/suite/probes/transferprobe.py"
# a node's own counters, read from its /metrics around each phase (the forwarded count is RELAY_METRIC)
NODE_COUNTERS = {"received": 'hopr_packets_count{type="received"}', "sent": 'hopr_packets_count{type="sent"}',
                 "rejected": "hopr_packet_rejected_count", "egress_dropped": "hopr_egress_ring_buffer_dropped"}


def timeout(knobs, connect_timeout=240):
    """Worst case per rung: connect, idle, both transfers at their cap, lead times, pauses, log reading; plus slack."""
    rungs = len(str(knobs.LADDER).split())
    per = connect_timeout + int(knobs.IDLE_S) + 2 * (int(knobs.CAP) + int(knobs.START_LEAD_S) + 90) + 2 * int(knobs.PAUSE_S) + 240
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


class Window:
    """What the machines did inside one window of a phase (MachineWatch.window)."""

    def __init__(self):
        self.machine_pct, self.node_pct, self.host_pct, self.host_max = {}, {}, {}, {}
        self.wire, self.udp, self.covered_s = {}, {}, None


class MachineWatch:
    """Every machine's counters over a phase (hosts.Sampler: CPU, each node's hoprd CPU, wire bytes, UDP errors, stamped
    with the machine's own clock), and afterwards what each did inside a window: busy % per machine (100 = all its
    cores), summarised per role label as the mean over the role's machines (host_pct) and the busiest one (host_max);
    each node's hoprd CPU (% of one core); wire bytes in and out and UDP error growth summed per role label."""

    def __init__(self, groups, interval, max_s):
        self.groups = groups
        self.samplers = {key: Sampler(host, pids.values(), interval, max_s) for key, (host, _, pids) in groups.items()}

    def start(self, timeout=20):
        for smp in self.samplers.values():
            smp.start()
        late = [key for key, ok in zip(self.samplers, _parallel(lambda smp: smp.wait_first(timeout), list(self.samplers.values()))) if not ok]
        if late:
            log(f"no sample within {timeout}s from: {late} (their CPU and wire figures will be missing)")
        return self

    def stop(self):
        _parallel(lambda smp: smp.stop(), list(self.samplers.values()))
        return self

    def window(self, a=0.0, b=float("inf")):
        w = Window()
        per_label, covered = {}, []
        for key, (_, label, pids) in self.groups.items():
            m = machine_window(self.samplers[key].samples, a, b, pids.values())
            covered.append(m["covered_s"])
            w.machine_pct[key] = m["cpu_pct"]
            if m["cpu_pct"] is not None:
                per_label.setdefault(label, []).append(m["cpu_pct"])
            for name, pid in pids.items():
                w.node_pct[name] = m["pid_pct"].get(int(pid)) if pid else None
            if m["wire_rx"] is not None and m["wire_tx"] is not None:
                rx, tx = w.wire.get(label, (0, 0))
                w.wire[label] = (rx + m["wire_rx"], tx + m["wire_tx"])
            if m["udp_errors"] is not None:
                tot = w.udp.setdefault(label, {x: 0 for x in UDP_ERRORS})
                for x, v in m["udp_errors"].items():
                    tot[x] = tot.get(x, 0) + v
        w.host_pct = {x: round(sum(v) / len(v), 1) for x, v in per_label.items()}
        w.host_max = {x: max(v) for x, v in per_label.items() if len(v) > 1}
        w.covered_s = min(covered) if covered else None
        return w


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


def bucket_rates(results, a, b, width):
    """Aggregate Mbit/s of all transfers in consecutive `width`-second buckets of [a, b]; a last partial bucket is
    dropped (a window shorter than one bucket is one bucket)."""
    width = float(width)
    if b <= a or width <= 0:
        return []
    if b - a < width:
        width = b - a
    out, t = [], a
    while t + width <= b + 1e-6:
        out.append(round(sum(interp(r["samples"], t + width) - interp(r["samples"], t) for r in results) * 8 / width / 1e6, 2))
        t += width
    return out


def longest_stall(r):
    """Longest stretch (s) between two increases of a transfer's byte count, first byte to last byte; None if it never moved."""
    best, last_t, last_b = 0.0, None, None
    for t, n in r.get("samples") or []:
        if last_b is None:
            last_t, last_b = t, n
        elif n > last_b:
            best, last_t, last_b = max(best, t - last_t), t, n
    return round(best, 2) if last_b is not None and r.get("first") is not None else None


def overlap(results, bucket_s=5):
    """The stretch in which every transfer was moving: [last first byte, first last byte]. Returns the window, its
    length, every client's Mbit/s inside it, their mean and minimum, the aggregate, the aggregate per bucket_s-second
    bucket (list, minimum, median, maximum), the longest stall of any transfer, and the spread of the starts. A
    transfer that never moved a byte has no window: then every rate is 0."""
    starts = [r["start"] for r in results if r.get("start") is not None]
    skew = round(max(starts) - min(starts), 3) if starts else None
    moving = [r for r in results if r.get("first") is not None and r.get("last") is not None]
    stalls = [x for x in (longest_stall(r) for r in results) if x is not None]
    base = {"start_skew_s": skew, "slowest_s": round(max((r["last"] - r["start"]) for r in moving), 2) if moving else None,
            "stall_max_s": max(stalls) if stalls else None, "buckets": [], "bucket_min": None, "bucket_median": None, "bucket_max": None}
    if not results or len(moving) < len(results):
        return dict(base, window=None, window_s=0, per_client_mbit=[0] * len(results), mean_mbit=0, min_mbit=0, agg_mbit=0)
    a, b = max(r["first"] for r in moving), min(r["last"] for r in moving)
    secs = b - a
    if secs <= 0:
        return dict(base, window=[a, b], window_s=0, per_client_mbit=[0] * len(results), mean_mbit=0, min_mbit=0, agg_mbit=0)
    per = [round((interp(r["samples"], b) - interp(r["samples"], a)) * 8 / secs / 1e6, 3) for r in results]
    bk = bucket_rates(results, a, b, bucket_s)
    if bk:
        base.update(buckets=bk, bucket_min=min(bk), bucket_median=round(st.median(bk), 2), bucket_max=max(bk))
    return dict(base, window=[round(a, 3), round(b, 3)], window_s=round(secs, 2), per_client_mbit=per,
                mean_mbit=round(st.mean(per), 3), min_mbit=min(per), agg_mbit=round(sum(per), 3))


def tunnel_shortfall(r):
    """Why a probe result does not show its bytes on the tunnel interface, or None when it does: the interface's rx
    (download) or tx (upload) counter must have grown by at least the transferred bytes."""
    t = r.get("tunnel")
    if not t:
        return "the probe was given no tunnel interface"
    moved = t.get("rx") if r.get("dir") == "down" else t.get("tx")
    if moved is None:
        return f"{t.get('iface')} was {'recreated' if t.get('recreated') else 'gone'} during the transfer"
    if moved < (r.get("bytes") or 0):
        return f"only {moved} B crossed {t.get('iface')} for {r.get('bytes')} B transferred"
    return None


def transfer_phase(group, direction, nbytes, k, target_ip, groups, save):
    """Every client of the rung runs one transfer, all starting at one common epoch second, while every machine is
    sampled. Returns (probe results, overlap summary, the machines inside the overlap window, the machines over the
    whole phase). Without an overlap window (a transfer that never moved) the first Window is the whole phase too."""
    watch = MachineWatch(groups, float(k.SAMPLE_S), int(k.CAP) + int(k.START_LEAD_S) + 240).start()
    try:
        start_at = time.time() + float(k.START_LEAD_S)
        base = (f"python3 {PROBE} --host {target_ip} --dir {direction} --bytes {int(nbytes)} --start-at {start_at:.3f} "
                f"--timeout {int(k.CAP)}")

        def one(cl):
            raw = cl.out(base + (f" --iface {cl.iface}" if cl.iface else ""), timeout=int(k.CAP) + int(k.START_LEAD_S) + 120)
            try:
                return json.loads(raw)
            except ValueError:
                return {"dir": direction, "want": nbytes, "bytes": 0, "complete": False, "samples": [], "start": None,
                        "first": None, "last": None, "code": None, "error": f"no probe output: {raw[-200:]!r}"}

        res = _parallel(one, group)
        time.sleep(float(k.SAMPLE_S) + 1)             # one more sample after the last byte
    finally:
        watch.stop()
    for cl, r in zip(group, res):
        save(cl, direction, r)
    ov = overlap(res, float(k.BUCKET_S))
    phase = watch.window()
    inside = watch.window(*ov["window"]) if ov["window"] and ov["window_s"] > 0 else phase
    return res, ov, inside, phase


def node_counts(cluster, ids, k):
    """{node id: {counter: value}} from each node's /metrics: forwarded (RELAY_METRIC) and NODE_COUNTERS."""
    names = dict(NODE_COUNTERS, forwarded=k.RELAY_METRIC)

    def one(i):
        text = cluster.metrics(i)
        return {name: telemetry_sum(text, metric) for name, metric in names.items()}

    return dict(zip(ids, _parallel(one, list(ids))))


def counts_delta(before, after):
    """{node id: {counter: growth}}; None where either reading is missing."""
    return {i: {name: (after[i][name] - v) if v is not None and after.get(i, {}).get(name) is not None else None
                for name, v in before[i].items()} for i in before}


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
    return {"hoprd": hoprd, "client": client.version(), "client_image": image, "machines": machines, "clock": clocks(groups)}


def clocks(groups):
    """Every machine's clock against the runner's. The probes and the samplers stamp with their own machine's clock and
    the overlap window is cut across them, so their disagreement is the error of every window edge. Per machine the
    offset is only known to lie between two bounds (the round trip of the question); `worst_s` is the largest offset
    any machine can have, `ntp` what each machine's NTP client reports about itself."""
    with ThreadPoolExecutor(max(len(groups), 1)) as ex:
        got = dict(zip(groups, ex.map(lambda g: g[0].clock(), groups.values())))
    bounds = {key: [lo, hi] for key, (lo, hi, _) in got.items()}
    known = [max(abs(lo), abs(hi)) for lo, hi, _ in got.values() if lo is not None]
    return {"worst_s": round(max(known), 3) if known else None, "unanswered": sorted(k for k, (lo, _, _) in got.items() if lo is None),
            "bounds_s": bounds, "ntp": {key: ntp for key, (_, _, ntp) in got.items() if ntp}}


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
    checks.row(kind="topology", topology={x: topo.get(x) for x in ("mode", "n", "relays", "exits", "servers", "clients", "created", "ready",
                                                                   "exit_channel_funding", "client_image", "hoprd_bin")}, stack=info)
    clock = info["clock"]
    checks.record(f"stack: hoprd {info['hoprd']['relay']} (relays) / {info['hoprd']['exit']} (exits); client {info['client']} "
                  f"(image {info['client_image']}); machines: " + " | ".join(f"{r}: {m}" for r, m in sorted(info["machines"].items()))
                  + f"; VPN servers: {topo.get('servers', len(tclients))}; clocks: no machine more than {clock['worst_s']} s from the runner"
                  + (f", {len(clock['unanswered'])} did not answer" if clock["unanswered"] else ""))
    ut_label = {"relay": "relays", "exit": "exits"}[under_test]
    if list(info["machines"]) == ["host"]:
        ut_label = "host"
    own_machines = ut_label in ("relays", "exits") and ut_label in info["machines"]    # the node under test has machines to itself
    log(f"{mode} topology: relays {topo['relays']}, exits {topo['exits']}; rungs {rungs}; under test: the {under_test}; "
        f"{k.DOWN_BYTES} B down, {k.UP_BYTES} B up, cap {k.CAP}s, idle {k.IDLE_S}s, pause {k.PAUSE_S}s, start lead {k.START_LEAD_S}s")
    tag = checks.test.split("-")[0].lower()
    node_ids = sorted(set(topo["relays"]) | set(topo["exits"]))

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

        # a rung outlasts the 900 s deadman at large n and CAP: idle, two capped transfers with their lead and the
        # samplers' start, the pause, and reading every client's log before the disconnect
        for cl in group:
            cl.deadman_cover(int(k.IDLE_S) + 2 * (int(k.CAP) + int(k.START_LEAD_S) + 40) + int(k.PAUSE_S) + 60 * n)
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

        # (3) downloads at once, every node's counters around them; the pause; uploads at once, counters again
        c0 = node_counts(cluster, node_ids, k)
        down, dov, down_cpu, down_phase = transfer_phase(group, "down", k.DOWN_BYTES, k, target.ip, groups, save)
        dcount = counts_delta(c0, node_counts(cluster, node_ids, k))
        up, uov, up_cpu, up_phase, ucount = [], None, None, None, None
        if k.UP_BYTES > 0:
            time.sleep(k.PAUSE_S)
            c0 = node_counts(cluster, node_ids, k)
            up, uov, up_cpu, up_phase = transfer_phase(group, "up", k.UP_BYTES, k, target.ip, groups, save)
            ucount = counts_delta(c0, node_counts(cluster, node_ids, k))
        errs = [cl.log_errors(since) for cl in group]
        dcmd = [cl.count_log(since, r"received socket command.*command=Disconnect") for cl in group]
        for c, cl in zip(group_t, group):
            cl.save_log(f"{tag}-n{n}-c{c['k']}", since)
        for cl in group:
            cl.disconnect()
        # (4) attribution: the relayed packets during each phase went through the relays. In single-exit a client's
        # return paths may use any relay the exit holds a channel to, so every relay counts and no share is scored.
        mine = sorted(topo["relays"]) if mode == "single-exit" else sorted({c["relay"] for c in group_t})

        def relayed(count):
            delta = {r: count[r]["forwarded"] for r in topo["relays"]}
            tot = sum(v for v in delta.values() if v is not None)
            on_mine = sum(delta[r] or 0 for r in mine)
            known = any(v is not None for v in delta.values())
            return delta, tot, on_mine, (round(100 * on_mine / tot, 1) if tot > 0 else None), known

        delta, tot, on_mine, attrib_pct, _ = relayed(dcount)
        udelta, utot, uon_mine, uattrib_pct, _ = relayed(ucount) if ucount else ({}, 0, 0, None, False)
        ut_nodes = sorted({c["relay"] for c in group_t}) if under_test == "relay" else sorted({c["exit"] for c in group_t})
        frames = [e.get("frame_discarded", 0) for e in errs]

        def wire(phase):
            return list(phase.wire[ut_label]) if phase is not None and ut_label in phase.wire else None

        # (5) rows and the rung's numbers
        strip = lambda r: {x: r.get(x) for x in ("want", "bytes", "acked", "counted", "complete", "code", "error", "start", "first",  # noqa: E731
                                                  "last", "done", "tunnel")}
        for i, (c, r, e, dc, ok) in enumerate(zip(group_t, down, errs, dcmd, connected)):
            u = up[i] if up else {}
            checks.row(n=n, kind="client", client=c["name"], relay=c["relay"], exit=c["exit"], down=strip(r), up=strip(u) if u else {},
                       down_overlap_mbit=dov["per_client_mbit"][i], up_overlap_mbit=uov["per_client_mbit"][i] if uov else None,
                       down_stall_s=longest_stall(r), up_stall_s=longest_stall(u) if u else None, connected_before=ok,
                       errors={x: e[x] for x in ("reconnects", "ping_timeouts", "no_surb", "frame_discarded", "reassembly_failed",
                                                 "warn_error_lines")},
                       disconnect_cmds=dc)
        rung = {
            "n": n,
            "down_avg_mbit": dov["mean_mbit"], "down_min_mbit": dov["min_mbit"], "down_agg_mbit": dov["agg_mbit"],
            "down_window_s": dov["window_s"], "down_start_skew_s": dov["start_skew_s"], "down_slowest_s": dov["slowest_s"],
            "down_bucket_mbit": [dov["bucket_min"], dov["bucket_median"], dov["bucket_max"]], "down_buckets": dov["buckets"],
            "down_stall_max_s": dov["stall_max_s"],
            "up_avg_mbit": uov["mean_mbit"] if uov else None, "up_min_mbit": uov["min_mbit"] if uov else None,
            "up_agg_mbit": uov["agg_mbit"] if uov else None, "up_window_s": uov["window_s"] if uov else None,
            "up_start_skew_s": uov["start_skew_s"] if uov else None, "up_slowest_s": uov["slowest_s"] if uov else None,
            "up_bucket_mbit": [uov["bucket_min"], uov["bucket_median"], uov["bucket_max"]] if uov else None,
            "up_buckets": uov["buckets"] if uov else None, "up_stall_max_s": uov["stall_max_s"] if uov else None,
            "bucket_s": float(k.BUCKET_S), "up_counted": sorted({u.get("counted") for u in up if u.get("counted")}),
            "under_test": under_test, "ut_nodes": [f"node-{i}" for i in ut_nodes],
            "cpu_window": ["overlap" if dov["window_s"] > 0 else "phase", ("overlap" if uov["window_s"] > 0 else "phase") if uov else None],
            "cpu_covered_s": [down_cpu.covered_s, up_cpu.covered_s if up_cpu else None],
            "ut_machine_cpu_down": down_cpu.host_pct.get(ut_label), "ut_machine_cpu_up": up_cpu.host_pct.get(ut_label) if up_cpu else None,
            "ut_process_cpu_down": {f"node-{i}": down_cpu.node_pct.get(f"node-{i}") for i in ut_nodes},
            "ut_process_cpu_up": {f"node-{i}": up_cpu.node_pct.get(f"node-{i}") for i in ut_nodes} if up_cpu else None,
            "ut_wire_down": wire(down_phase), "ut_wire_up": wire(up_phase),
            "ut_udp_errors_down": down_phase.udp.get(ut_label), "ut_udp_errors_up": up_phase.udp.get(ut_label) if up_phase else None,
            "ut_counts_down": {f"node-{i}": dcount[i] for i in ut_nodes},
            "ut_counts_up": {f"node-{i}": ucount[i] for i in ut_nodes} if ucount else None,
            "udp_errors_down": down_phase.udp, "udp_errors_up": up_phase.udp if up_phase else None,
            "wire_down": {x: list(v) for x, v in down_phase.wire.items()},
            "wire_up": {x: list(v) for x, v in up_phase.wire.items()} if up_phase else None,
            "host_cpu_down_pct": down_cpu.host_pct, "host_cpu_up_pct": up_cpu.host_pct if up_cpu else None,
            "host_cpu_down_max": down_cpu.host_max, "host_cpu_up_max": up_cpu.host_max if up_cpu else None,
            "machine_cpu_down_pct": down_cpu.machine_pct, "machine_cpu_up_pct": up_cpu.machine_pct if up_cpu else None,
            "relay_cpu_down_pct": {f"node-{r}": down_cpu.node_pct.get(f"node-{r}") for r in sorted({c["relay"] for c in group_t})},
            "exit_cpu_down_pct": {f"node-{x}": down_cpu.node_pct.get(f"node-{x}") for x in sorted({c["exit"] for c in group_t})},
            "relay_packets_down": {f"node-{r}": v for r, v in delta.items()}, "attrib_pct": attrib_pct,
            "relay_packets_up": {f"node-{r}": v for r, v in udelta.items()}, "attrib_pct_up": uattrib_pct,
            "frames_discarded": sum(frames), "frames_discarded_max": max(frames) if frames else 0,
            "reconnects": sum(e["reconnects"] for e in errs), "ping_timeouts": sum(e["ping_timeouts"] for e in errs),
            "disconnect_cmds": sum(dcmd), "connect_ms": [r.get("connect_ms") for r in conn],
            "incomplete_down": sum(1 for r in down if not r.get("complete")),
            "incomplete_up": sum(1 for u in up if not u.get("complete")),
        }
        table.append(rung)
        checks.row(kind="rung", **rung)
        # (6) verdicts: every transfer within CAP; every transfer on its tunnel; the traffic crossed the relays, both
        # ways; the wire of the node under test carried it
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
        short = [(c["name"], r.get("dir"), tunnel_shortfall(r)) for c, r in list(zip(group_t, down)) + list(zip(group_t, up)) if r.get("complete")]
        blind = [x for x in short if x[2] and x[2].startswith("the probe was given")]
        off = [f"{name} {d}: {reason}" for name, d, reason in short if reason and not reason.startswith("the probe was given")]
        if off:
            checks.failed(f"n={n}: {len(off)} complete transfer(s) not accounted for on the tunnel interface: " + "; ".join(off))
        elif blind:
            checks.warn(f"n={n}: {len(blind)} transfer(s) ran without a tunnel interface to check (no wg interface found after connect)")
        elif short:
            checks.passed(f"n={n}: every complete transfer's bytes are on its client's tunnel interface counters ({len(short)} transfers)")
        for phase_name, res, got, got_tot, pct, count in (("download", down, on_mine, tot, attrib_pct, dcount),
                                                           ("upload", up, uon_mine, utot, uattrib_pct, ucount)):
            if not res:
                continue
            if not relayed(count)[4]:
                checks.warn(f"n={n}: no {k.RELAY_METRIC} counts from the relays during the {phase_name}; attribution not checked (RELAY_METRIC)")
                continue
            relays_named = ['node-%d' % r for r in mine]
            want = attribution_floor(sum(r.get("bytes") or 0 for r in res), k.PKT_BYTES_MAX)
            checks.assert_min(f"n={n}: packets forwarded by {relays_named if len(relays_named) <= 5 else str(len(relays_named)) + ' relays'} "
                              f"during the {phase_name} (floor: one per {k.PKT_BYTES_MAX} B transferred = {want})", got, "packets", "floor", want)
            if len(topo["relays"]) > 1 and mode != "single-exit":
                checks.assert_min(f"n={n}: share of relayed packets on the rung's relay(s) {relays_named} during the {phase_name} "
                                  f"({got} of {got_tot})", pct, "%", "ATTRIB_MIN_PCT", k.ATTRIB_MIN_PCT)
            else:
                checks.record(f"n={n}: share of relayed packets during the {phase_name} not scored "
                              f"({'one relay' if len(topo['relays']) == 1 else 'return paths not pinned'}; {got} forwarded)")
        for phase_name, res, w in (("download", down, rung["ut_wire_down"]), ("upload", up, rung["ut_wire_up"])):
            if not res or not own_machines:
                continue
            need = sum(r.get("bytes") or 0 for r in res if r.get("complete"))
            if w is None:
                checks.warn(f"n={n}: no wire counters from the {under_test} machine(s) during the {phase_name}; its wire not checked")
                continue
            rx, tx = w
            # a relay takes every byte in and sends it on; an exit sends a download out and takes an upload in
            carried = min(rx, tx) if under_test == "relay" else (tx if phase_name == "download" else rx)
            checks.assert_min(f"n={n}: bytes on the wire of the {under_test} machine(s) during the {phase_name} (rx {rx}, tx {tx}; floor: "
                              f"the {need} B of the complete transfers)", carried, "B", "floor", need)
        sent_floor = attribution_floor(sum(r.get("bytes") or 0 for r in down), k.PKT_BYTES_MAX)
        ut_d, ut_u = rung["ut_counts_down"], rung["ut_counts_up"] or {}
        if under_test == "exit":
            low = [f"{node} sent {c.get('sent')} packets during the download (one per {k.PKT_BYTES_MAX} B would be {sent_floor})"
                   for node, c in ut_d.items() if c.get("sent") is not None and c["sent"] < sent_floor]
            if low:
                checks.warn(f"n={n}: the exit's own packet counter is below the payload: " + "; ".join(low))
        checks.record(f"n={n}: overlap rates, down {rung['down_avg_mbit']} Mbit/s per client (min {rung['down_min_mbit']}), aggregate "
                      f"{rung['down_agg_mbit']} over {rung['down_window_s']}s; up {rung['up_avg_mbit']} per client (min {rung['up_min_mbit']}), "
                      f"aggregate {rung['up_agg_mbit']} over {rung['up_window_s']}s; {under_test} {rung['ut_nodes']}: machine "
                      f"{rung['ut_machine_cpu_down']} % / {rung['ut_machine_cpu_up']} %, process {rung['ut_process_cpu_down']} / "
                      f"{rung['ut_process_cpu_up']} (down / up; process % of one core; CPU over the {rung['cpu_window'][0]} window)")
        checks.record(f"n={n}: steadiness, aggregate per {k.BUCKET_S}-s bucket min/median/max: down {_mmm(rung['down_bucket_mbit'])}, "
                      f"up {_mmm(rung['up_bucket_mbit'])} Mbit/s; longest stall of a transfer {rung['down_stall_max_s']}s down / "
                      f"{rung['up_stall_max_s']}s up; frames discarded by the clients {rung['frames_discarded']} "
                      f"(most on one client {rung['frames_discarded_max']}); uploads counted as {', '.join(rung['up_counted']) or '-'}")
        checks.record(f"n={n}: {under_test} {rung['ut_nodes']}: packets down {_counts(ut_d)}; up {_counts(ut_u)}; machine UDP errors "
                      f"down {_udp(rung['ut_udp_errors_down'])} / up {_udp(rung['ut_udp_errors_up'])}; wire bytes (rx, tx) down "
                      f"{rung['ut_wire_down']} / up {rung['ut_wire_up']}")
    if table:
        write_table(run, checks.test, mode, topo, k, table, info, under_test)
    return table


def _mmm(v):
    return "n/a" if not v or v[0] is None else f"{v[0]}/{v[1]}/{v[2]}"


def _counts(by_node):
    """{"node-10": {"received": 5, "sent": 7, ...}} -> "node-10 received 5, sent 7, forwarded 0, rejected 0, egress_dropped 0"."""
    if not by_node:
        return "n/a"
    order = ("received", "sent", "forwarded", "rejected", "egress_dropped")
    return "; ".join(f"{node} " + ", ".join(f"{x} {c.get(x)}" for x in order if c.get(x) is not None) for node, c in by_node.items())


def _udp(d):
    """UDP error growth as text: "0" when every counter stood still, else the ones that moved."""
    if d is None:
        return "n/a"
    moved = {x: v for x, v in d.items() if v}
    return "0" if not moved else ", ".join(f"{x} {v}" for x, v in moved.items())


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
             f"- client: {info['client']} (image {info['client_image']})",
             f"- VPN servers: {topo.get('servers', len(topo['clients']))}"]
    lines += [f"- {role} machines: {spec}" for role, spec in sorted(info["machines"].items())]
    clock = info.get("clock") or {}
    lines += [f"- clocks: no machine more than {clock.get('worst_s')} s from the runner (upper bound from one ssh round trip)",
              "", f"{k.DOWN_BYTES} B down, then {k.UP_BYTES} B up, on every client at once (one common start), cap {k.CAP} s; "
              f"idle {k.IDLE_S} s after connect, {k.PAUSE_S} s between phases and rungs. Rates are over the overlap: from the last "
              f"transfer's first byte to the first transfer's last byte; upload bytes count when the target's TCP acknowledged them. "
              f"Buckets: the aggregate per {k.BUCKET_S} s inside the overlap, min / median / max. Under test: the {under_test}; machine "
              f"CPU is % of all its cores, process CPU % of one core, both over the overlap window. UDP errors: growth of the {under_test} "
              f"machine's InErrors, RcvbufErrors, SndbufErrors and MemErrors over the phase (0 = none moved). Frames discarded: by all "
              f"clients of the rung, from their logs, both phases.", "",
              f"| clients | down per client, mean (min) Mbit/s | down aggregate | down buckets | up per client, mean (min) | up aggregate "
              f"| up buckets | {under_test} machine CPU down / up | {under_test} process CPU down / up | {under_test} machine UDP errors down / up "
              f"| longest stall down / up s | frames discarded | reconnects (ping timeouts) |",
              "|" + " --- |" * 13]
    for r in table:
        lines.append(f"| {r['n']} | {r['down_avg_mbit']} ({r['down_min_mbit']}) | {r['down_agg_mbit']} | {_mmm(r['down_bucket_mbit'])} "
                     f"| {r['up_avg_mbit']} ({r['up_min_mbit']}) | {r['up_agg_mbit']} | {_mmm(r['up_bucket_mbit'])} "
                     f"| {r['ut_machine_cpu_down']} % / {r['ut_machine_cpu_up']} % "
                     f"| {_proc(r['ut_process_cpu_down'])} / {_proc(r['ut_process_cpu_up'])} "
                     f"| {_udp(r['ut_udp_errors_down'])} / {_udp(r['ut_udp_errors_up'])} "
                     f"| {r['down_stall_max_s']} / {r['up_stall_max_s']} | {r['frames_discarded']} | {r['reconnects']} ({r['ping_timeouts']}) |")
    (run / f"{test}.md").write_text("\n".join(lines) + "\n")
