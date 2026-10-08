"""T36-relay-machine-class (gate): does one relay on a given machine class (an infrastructure provider and one of its
machine sizes, e.g. DigitalOcean's 4-vCPU CPU-optimized c-4) still carry what that class carried when it was
calibrated? The T34-single-relay-scaling ladder, held against absolute thresholds per class and rung from
tests/machine_classes.toml. A ladder of classes (digitalocean:cpu-optimized = c-2, c-4, c-8, c-16) is one run of this
test per class, one relay machine after another: multihost/machine_ladder.py.

Shape (suitelib/relaybench.py, `shared` topology): rungs of LADDER="1 2 3 4 5 6" clients, each with its own exit, all
through the one relay, download DOWN_BYTES=100000000 on every client at once (one common start, START_LEAD_S=5),
UP_BYTES=0 (the classes were calibrated on downloads), CAP=300 s, IDLE_S=10 after connecting, PAUSE_S=10 between rungs,
PING=1: every transfer pings the target through the tunnel once a second for its length. The relay's class is
MACHINE="" (`<provider>:<size>`, empty: detected), the table CLASSES="" (empty: tests/machine_classes.toml).

Before the ladder, the relay machine is probed (vCPUs, CPU model, MemTotal, and which provider's metadata service
answers, each provider's `detect` in the table) and held against the class: with MACHINE set, a machine that is not
on that provider, has another vCPU count or MemTotal outside 0.90-1.0 of the class's memory_gb FAILs the test before a
byte moves (the thresholds would be another machine's); another CPU model than the calibrated one is a WARN (a
provider may move a size to new hardware). With MACHINE empty the class is the one table entry the machine matches
on all of these; none or several SKIP (name it with T36_MACHINE). SKIP too when the relay shares its machine with
another role (a single-machine stack, or a hosts file that puts it there) or on any topology but `shared`.

Scored per rung (both only where the class has a threshold for that rung; a rung past the end of its lists is
uncalibrated, RECORDED): the aggregate download over the overlap (the window in which every client was moving)
>= agg_min_mbit[n]; the median round trip of all the rung's pings (nearest rank, lost echoes excluded) <=
ping_p50_max_ms[n]. Both are absolute, per class and rung, derived from the calibration sweep by one rule (in
suitelib/machineclass.py derive(): 75 % of the lowest measured aggregate rounded down to 0.5 Mbit/s; twice the highest
measured median rounded up to 100 ms). The rates are only comparable on the rig they were calibrated on, so the scores
are RECORDED with a WARN instead when a knob in the table's [calibration] knobs differs, or when the clients' or the
exits' machines have fewer vCPUs than the calibration's or are fewer than the top rung's clients (each client and
each exit had a machine of its own). A transfer that does not complete within CAP is recorded, not failed: a small
class cannot move n x 100 MB in 300 s, and its rate over the overlap is still measured. Every other check of the
ladder holds as in T34-single-relay-scaling and FAILs as there: the channel graph before every rung, every client
connects, every complete transfer's bytes on its tunnel interface, the relay forwarded at least one packet per
PKT_BYTES_MAX=1000 transferred bytes, its machine's wire carried the transferred bytes.

How to read a FAIL. The ceiling of one relay is set by its CPU (2026-10-07: hoprd levels off at 60-69 % of every
CPU-optimized size; c-N behaves like N/2 physical cores), so a floor that fails on every size of a ladder is the
product; one that fails on one size only, the droplet: read the machine's steal time and its benchmark (the ladder
keeps both per droplet) before blaming hoprd. Latency degrades before throughput: the ping ceiling usually trips first.
At n=1 the large classes are client-bound (about 9.5 Mbit/s whatever the relay), so their n=1 floor guards the
client path, not the relay. A rung that FAILs on connect alone was, in 3 of 9 bring-ups of the calibration sweep, one
client that never resolved the packet keys of its exit's channel ("could not find packet keys for channel
endpoints"): the rig, not the relay; restart that client's stack and rerun.

Setup: one relay machine of the class, CLIENTS clients and exits on machines of their own (`just multihost-up HOSTS
shared 6`), then `T36_MACHINE=digitalocean:c-4 just multihost-test t36`; or the whole ladder on DigitalOcean:
`python3 multihost/machine_ladder.py run --name NAME --ladder digitalocean:cpu-optimized`."""
from suitelib import machineclass, relaybench, relaytopo

TEST = "T36-relay-machine-class"
KIND = "gate"
GROUP = "relayscale"
KNOBS = dict(relaybench.SCALING_KNOBS, LADDER="1 2 3 4 5 6", UP_BYTES=0, PING=1, MACHINE="", CLASSES="")


def TIMEOUT(knobs):
    return relaybench.timeout(knobs) + 300


def probe(groups, table):
    """{machine key: (role label, probed machine or None)} for every machine of the stack."""
    cmd = machineclass.probe_cmd(table)
    out = relaybench._parallel(lambda g: (g[1], machineclass.parse_probe(g[0].out(cmd, timeout=60))), list(groups.values()))
    return dict(zip(groups, out))


def detect(table, seen):
    """The classes the machine matches, best first: those with no mismatch at all, else those off only in CPU model."""
    exact, near = [], []
    for name in machineclass.known(table):
        provider, _, entry = machineclass.lookup(table, name)
        hard, soft = machineclass.mismatch(provider, entry, seen)
        if not hard:
            (near if soft else exact).append(name)
    return exact or near


def rig_drift(table, machines, top):
    """Why the clients' and exits' machines are not the calibration's (a list; empty: they are)."""
    cal = table.get("calibration", {})
    out = []
    for role in ("clients", "exits"):
        want = cal.get(role, {}).get("vcpus")
        mine = [m for label, m in machines.values() if role in label.split("+")]
        if any(role in label.split("+") and "relays" in label.split("+") for label, _ in machines.values()):
            out.append(f"{role} share a machine with the relay")
        if len(mine) < top:
            out.append(f"{len(mine)} {role} machine(s) for {top} clients (calibrated: one each)")
        small = [m["vcpus"] for m in mine if m and want and m["vcpus"] < int(want)]
        if small:
            out.append(f"{role} machine(s) with {sorted(set(small))} vCPUs (calibrated: {want})")
        if any(m is None for m in mine):
            out.append(f"a {role} machine did not answer the probe")
    return out


def test_relay_machine_class(cfg, run, cluster, target, checks, knobs):
    k = knobs
    table = machineclass.load(k.CLASSES or None)
    topo = relaytopo.load(cfg.config_dir)
    if topo is None or topo.get("mode") != "shared":
        checks.skip(f"needs the 'shared' relay topology on machines of their own (just multihost-up HOSTS shared N); CONFIG_DIR holds "
                    + (f"a '{topo.get('mode')}' topology" if topo else "no relay topology"))
    status = cluster.status() or {}
    groups = relaybench.cpu_groups(cfg, topo, status)
    machines = probe(groups, table)
    relay = [(key, m) for key, (label, m) in machines.items() if label == "relays"]
    if len(relay) != 1:
        where = sorted({label for label, _ in machines.values()})
        checks.skip(f"the relay needs a machine of its own (machines: {where}); just multihost-up HOSTS shared N")
    key, seen = relay[0]
    if seen is None:
        checks.failed(f"the relay machine {key} did not answer the machine probe")
        return
    desc = f"{seen['vcpus']} vCPU, {seen['cpu_model']}, {seen['mem_kb'] / 1024 / 1024:.2f} GiB, provider {seen['providers'] or 'unknown'}"
    if k.MACHINE:
        try:
            provider, size, entry = machineclass.lookup(table, k.MACHINE)
        except KeyError as e:
            checks.failed(f"MACHINE={k.MACHINE}: {e.args[0]}")
            return
        hard, soft = machineclass.mismatch(provider, entry, seen)
        if hard:
            checks.failed(f"the relay machine {key} is not {k.MACHINE} ({desc}): " + "; ".join(hard) + "; ladder not run")
            return
        name = k.MACHINE
    else:
        found = detect(table, seen)
        if len(found) != 1:
            checks.skip(f"the relay machine ({desc}) matches {'no' if not found else 'more than one'} calibrated class "
                        f"({', '.join(found) or 'known: ' + ', '.join(machineclass.known(table))}); name it with T36_MACHINE")
        name = found[0]
        provider, size, entry = machineclass.lookup(table, name)
        soft = machineclass.mismatch(provider, entry, seen)[1]
    for s in soft:
        checks.warn(f"relay machine {key}: {s}; the thresholds may not be this hardware's")
    rungs = k.numbers("LADDER", lo=1, ints=True)
    cal = table.get("calibration", {})
    drift = [f"{x}={now} (calibrated {was})" for x, was, now in machineclass.knob_drift(table, k)]
    rig = rig_drift(table, machines, max(rungs))
    checks.row(kind="machine_class", machine=name, relay_machine=key, probed=seen, calibration={x: cal.get(x) for x in ("source", "hoprd", "client", "server")},
               knob_drift=drift, rig_drift=rig, others={m: v for m, v in machines.items() if m != key})
    checks.record(f"relay class {name} on {key} ({desc}{', set by MACHINE' if k.MACHINE else ', detected'}); calibrated on "
                  f"{cal.get('source')}: hoprd {cal.get('hoprd')}, client {cal.get('client')}")
    scored = not drift and not rig
    if not scored:
        checks.warn("rates not scored, the rig is not the calibration's: " + "; ".join(drift + rig))
    table_rows = relaybench.run_ladder(cfg, run, cluster, target, checks, knobs, "shared", under_test="relay", complete_gate=False)
    lines = ["", f"## {name}: against tests/machine_classes.toml", "",
             "| clients | aggregate Mbit/s | agg_min_mbit | calibrated | ping median ms | ping_p50_max_ms | calibrated | ping loss % |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in table_rows or []:
        n = r["n"]
        agg_min, ping_max = machineclass.thresholds(entry, n)
        agg = r["down_agg_mbit"]
        ping = r.get("down_ping") or {}
        p50 = ping.get("p50_ms")
        cal_agg = [run_[n - 1] for run_ in entry.get("measured_agg_mbit", []) if n <= len(run_)]
        cal_ping = [run_[n - 1] for run_ in entry.get("measured_ping_p50_ms", []) if n <= len(run_)]
        lines.append(f"| {n} | {agg} | {agg_min if agg_min is not None else '-'} | {', '.join(map(str, cal_agg)) or '-'} | {p50} "
                     f"| {ping_max if ping_max is not None else '-'} | {', '.join(map(str, cal_ping)) or '-'} | {ping.get('loss_pct')} |")
        what = f"n={n}: {name} relay"
        if agg_min is None:
            checks.record(f"{what}: aggregate {agg} Mbit/s, ping median {p50} ms; rung not calibrated for this class")
            continue
        if not scored:
            checks.record(f"{what}: aggregate {agg} Mbit/s (agg_min_mbit {agg_min}), ping median {p50} ms (ping_p50_max_ms {ping_max}); not scored")
            continue
        checks.assert_min(f"{what}: aggregate download over the overlap (calibrated {', '.join(map(str, cal_agg))})", agg, "Mbit/s",
                          f"agg_min_mbit[{name} n={n}]", agg_min)
        if ping_max is None:
            continue
        if p50 is None:
            checks.failed(f"{what}: no ping answered during the download (PING={k.PING}); ping_p50_max_ms[{name} n={n}]={ping_max} not met")
            continue
        checks.assert_max(f"{what}: median ping through the tunnel during the download (calibrated {', '.join(map(str, cal_ping))}; "
                          f"{ping.get('lost')} of {ping.get('sent')} lost)", p50, "ms", f"ping_p50_max_ms[{name} n={n}]", ping_max)
    md = run / f"{TEST}.md"
    if md.exists():
        md.write_text(md.read_text() + "\n".join(lines) + "\n")
