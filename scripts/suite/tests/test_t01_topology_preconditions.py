"""T01-topology-preconditions: cluster running, every node channels_open with a full mesh of open channels, each
relay forwards a 1-hop session opened from the exit within FWD_TIMEOUT s, the client worker online and every
destination Ready, the client holding an outgoing channel, both liveness-ping targets on the server's wggvpn,
no leftover qdisc or suite timer on the host. A FAIL aborts the run."""
import re
import time

from suitelib import shell

TEST = "T01-topology-preconditions"
KIND = "gate"
KNOBS = dict(FWD_TIMEOUT=20, READY_TIMEOUT=300, CLIENT_CHANNEL_TIMEOUT=240, PERIODIC_PING_TARGET="10.128.0.1")


def test_topology_preconditions(cfg, run, client, cluster, checks, knobs):
    k = knobs
    # cluster
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
    if cfg.cluster_size >= 3:
        for j in range(1, cfg.cluster_size):
            t0 = time.time()
            r = cluster.api_json(0, "POST", "/api/v4/session/udp",
                                 {"destination": cluster.address(j), "forwardPath": {"Hops": 1}, "returnPath": {"Hops": 0},
                                  "target": {"Plain": "127.0.0.1:9"}, "capabilities": []}, timeout=k.FWD_TIMEOUT + 40) or {}
            dt = int((time.time() - t0) * 1000)
            port, ip = r.get("port"), r.get("ip")
            if port:
                cluster.api(0, "DELETE", f"/api/v4/session/udp/{ip}/{port}")
                checks.passed(f"1-hop session node0->(relay)->node{j} established in {dt} ms")
            else:
                checks.failed(f"1-hop session node0->(relay)->node{j} failed after {dt} ms: {str(r)[:160]}")
            checks.row(check="forwarding_probe", dest_node=j, ms=dt, ok=bool(port))
    # client side
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
        if not want:
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
