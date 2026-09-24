"""T01-topology-preconditions (gate): is the stack in the state every later test assumes? Reads the localcluster
status and every node's channel set, opens a 1-hop UDP session from the exit through each relay, waits for the
client worker, for DEST to be Ready and for the client's own outgoing channel, checks both liveness-ping targets
against the server's wggvpn addresses, and looks for leftover netem qdiscs and suite timers on the host. The
effective client config is recorded against the shipped defaults, not asserted. A FAIL aborts the run: a number
measured through a broken precondition looks like a finding.

Pass iff: cluster state is running; every node reports channels_open with at least CLUSTER_SIZE-1 outgoing channels
Open; with CLUSTER_SIZE >= 3 a 1-hop session from node 0 to every other node establishes within FWD_TIMEOUT s (a
later success fails naming the duration; the session is deleted either way); the client worker is online within
120 s; DEST is Ready within READY_TIMEOUT s; the client holds an outgoing channel within CLIENT_CHANNEL_TIMEOUT s;
both the configured [connection.ping] address and PERIODIC_PING_TARGET (the address a client <= 0.96.3 pings from
tunnel_ping_loop whatever the config says) are on the server's wggvpn; no netem qdisc on the host. WARN only:
another destination not Ready, an armed suite or deadman timer.

Why: a relay that pings at 6 ms can forward nothing (a production relay silently rejected tickets for 2.5 hours and
invalidated every run in the window), so the probe opens a session instead of pinging. A missing liveness-ping
target makes every session reconnect every ~85 s; two full runs were read as a load defect before that was found.
Not checked: channel balance, foreign peers on the exit, identity double-runs, announced addresses."""
import re
import time

from suitelib.client import clients_running
from suitelib import shell

TEST = "T01-topology-preconditions"
KIND = "gate"
KNOBS = dict(FWD_TIMEOUT=20, READY_TIMEOUT=300, CLIENT_CHANNEL_TIMEOUT=240, PERIODIC_PING_TARGET="10.128.0.1")


def test_topology_preconditions(cfg, run, client, cluster, checks, knobs):
    k = knobs
    has_cluster = cluster.available()
    if not has_cluster:
        checks.record("no localcluster (production-network run): cluster, forwarding and server checks skipped")
    # cluster
    if has_cluster:
        state = cluster.state()
        checks.verdict(state == "running", f"cluster state '{state}'")
        for i in range(cfg.cluster_size):
            ns = cluster.field(i, "state")
            checks.verdict(ns == "channels_open", f"node {i} state '{ns}'")
        # channels open in both directions between every pair (localcluster full mesh)
        for i in range(cfg.cluster_size):
            n = len(cluster.open_outgoing(i))
            checks.verdict(n >= cfg.cluster_size - 1, f"node {i} has {n} open outgoing channels")
    # forwarding probe: from node 0, a session with Hops=1 to node j is forced through the remaining node(s)
    if has_cluster and cfg.cluster_size >= 3:
        for j in range(1, cfg.cluster_size):
            t0 = time.time()
            r = cluster.api_json(0, "POST", "/api/v4/session/udp",
                                 {"destination": cluster.address(j), "forwardPath": {"Hops": 1}, "returnPath": {"Hops": 0},
                                  "target": {"Plain": "127.0.0.1:9"}, "capabilities": []}, timeout=k.FWD_TIMEOUT + 40) or {}
            dt = int((time.time() - t0) * 1000)
            port, ip = r.get("port"), r.get("ip")
            if port:
                cluster.api(0, "DELETE", f"/api/v4/session/udp/{ip}/{port}")
            ok = bool(port) and dt <= k.FWD_TIMEOUT * 1000
            if ok:
                checks.passed(f"1-hop session node0->(relay)->node{j} established in {dt} ms (FWD_TIMEOUT={k.FWD_TIMEOUT}s)")
            elif port:
                checks.failed(f"1-hop session node0->(relay)->node{j} took {dt} ms > FWD_TIMEOUT={k.FWD_TIMEOUT}s")
            else:
                checks.failed(f"1-hop session node0->(relay)->node{j} failed after {dt} ms: {str(r)[:160]}")
            checks.row(check="forwarding_probe", dest_node=j, ms=dt, ok=ok)
    # client side: every client container the run will use needs its tools sidecar (curl, ping, ip, the probes run
    # there; the upstream client image has none of them, and a run without the sidecar reads zero bytes, no error)
    for c in clients_running(cfg, run):
        if c.tools_exists():
            checks.passed(f"{c.name}: tools sidecar {c.tools} running")
        elif cfg.no_cluster:
            checks.warn(f"{c.name}: no tools sidecar {c.tools}; commands run in the client container (--no-cluster)")
        else:
            checks.failed(f"{c.name}: tools sidecar {c.tools} not running (start the client with `just client-start`)")
    if not client.wait_worker(120):
        checks.failed("client worker offline")
    for d in client.destinations():
        if client.dest_is_ready(d):
            checks.passed(f"destination {d} Ready")
        else:
            checks.warn(f"destination {d}: {client.dest_health_line(d)[:120]}")
    if not client.wait_dest_ready(cfg.dest, k.READY_TIMEOUT):
        checks.failed(f"primary destination {cfg.dest} not Ready within {k.READY_TIMEOUT}s: {client.dest_health_line(cfg.dest)[:120]}")
    # client channel pin: the client opens its outgoing channels on-chain asynchronously after the worker comes
    # up, so this polls rather than sampling once (a suite launched 15 s after `just up` failed here on a healthy stack)
    nch, waited = 0, 0
    while waited < k.CLIENT_CHANNEL_TIMEOUT:
        nch = client.channels_out()
        if nch >= 1:
            break
        time.sleep(5)
        waited += 5
    if nch >= 1:
        checks.passed(f"client holds {nch} outgoing channel(s)" + (f" (after {waited}s wait)" if waited else ""))
    else:
        checks.failed(f"client holds no outgoing channel after {k.CLIENT_CHANNEL_TIMEOUT}s")
    # effective config vs shipped defaults: record the diff, never trust the file alone
    cfg_file = cfg.config_dir / "client.toml"
    effective = open(cfg_file).read()
    (run / "client.toml.effective").write_text(effective)
    try:
        defaults = open(run / "client.toml.defaults").read()
        norm = lambda s: sorted(re.sub(r"\s+", "", l) for l in s.splitlines())   # noqa: E731
        a, b = norm(effective), norm(defaults)
        diff_lines = len(set(a) ^ set(b))
    except FileNotFoundError:
        diff_lines = 0
    checks.row(check="effective_config", diff_lines=diff_lines)
    # tunnel liveness target. The client's periodic tunnel ping (10 s interval, 3 misses = reconnect) targets the
    # hardcoded default 10.128.0.1 in client <= 0.96.3 regardless of [connection.ping].address; if the server does
    # not hold that address every session reconnects every ~85 s and every longer test reads as a load defect.
    m = re.search(r"^\[connection\.ping\](.*?)(?=^\[|\Z)", effective, re.S | re.M)
    cfg_ping = ""
    if m:
        mm = re.search(r'^address\s*=\s*"([^"]+)"', m.group(1), re.M)
        cfg_ping = mm.group(1) if mm else ""
    srv = shell.out(["docker", "exec", cfg.server, "ip", "-4", "-o", "addr", "show", "dev", "wggvpn"], timeout=30)
    srv_addrs = [l.split()[3].split("/")[0] for l in srv.splitlines() if len(l.split()) > 3]
    for want in (k.PERIODIC_PING_TARGET, cfg_ping):
        if not want or not has_cluster:
            continue
        if want in srv_addrs:
            checks.passed(f"liveness-ping target {want} is an address on the server's wggvpn")
        else:
            checks.failed(f"liveness-ping target {want} is NOT on the server's wggvpn ({' '.join(srv_addrs)}) - the client "
                          f"will reconnect every ~85 s and every test longer than that fails on it; set SERVER_PING_ALIAS "
                          f"or fix the client's tunnel_ping_loop")
    checks.row(check="liveness_target", periodic=k.PERIODIC_PING_TARGET, configured=cfg_ping, server_wggvpn=" ".join(srv_addrs))
    # host hygiene
    q = cluster.netem_count()
    checks.verdict(q == 0, f"{q} netem qdisc(s) on host" if q else "no netem qdisc on host")
    timers = shell.out("systemctl list-timers --all 2>/dev/null", timeout=30)
    tm = sum(1 for l in timers.splitlines() if re.search(r"deadman|suite-", l, re.I))
    if tm == 0:
        checks.passed("no armed suite timers")
    else:
        checks.warn(f"{tm} suite/deadman timers armed")
    checks.row(kind="summary", fail=len(checks.failures))
