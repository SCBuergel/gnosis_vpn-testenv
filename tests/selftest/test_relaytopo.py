"""Offline checks for the relay-scaling topologies (suitelib/relaytopo.py): layout, per-client config, channel check."""
import json
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from suitelib import relaytopo  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "templates"


def addr(i):
    return "0x" + f"{i:040x}"


def status(n_nodes, n_extras):
    return {"nodes": [{"id": i, "address": addr(i)} for i in range(n_nodes)],
            "extras": [{"id": i, "address": addr(100 + i)} for i in range(n_extras)]}


def test_paired_layout():
    lay = relaytopo.layout("paired", 3)
    assert lay["cluster_size"] == 6 and lay["relays"] == [0, 1, 2] and lay["exits"] == [3, 4, 5]
    assert [(c["name"], c["relay"], c["exit"], c["server"], c["dest"]) for c in lay["clients"]] == [
        ("gnosis_vpn-client", 0, 3, 0, "node-3"), ("gnosis_vpn-client-2", 1, 4, 1, "node-4"), ("gnosis_vpn-client-3", 2, 5, 2, "node-5")]


def test_shared_layout():
    lay = relaytopo.layout("shared", 3)
    assert lay["cluster_size"] == 4 and lay["relays"] == [0] and lay["exits"] == [1, 2, 3]
    assert {c["relay"] for c in lay["clients"]} == {0}


def test_layout_refuses_bad_input():
    with pytest.raises(ValueError):
        relaytopo.layout("star", 2)
    with pytest.raises(ValueError):
        relaytopo.layout("paired", 0)


def test_client_config_pins_one_relay_and_own_server():
    topo = relaytopo.with_addresses(relaytopo.layout("paired", 2), status(4, 2))
    c = topo["clients"][1]
    cfg = tomllib.loads(relaytopo.render_client_config(TEMPLATES, c))
    assert list(cfg["destinations"]) == ["node-3"]
    assert cfg["destinations"]["node-3"]["address"] == addr(3)
    assert cfg["destinations"]["node-3"]["path"] == {"hops": 1}
    assert cfg["connection"]["bridge"]["target"] == "127.0.0.1:8001"
    assert cfg["connection"]["wg"]["target"] == "127.0.0.1:51822"
    assert cfg["strategy"]["min_open_channels"] == 1 and cfg["strategy"]["target_open_channels"] == 1
    assert cfg["strategy"]["channel_allowlist"] == {"enabled": True, "peers": [addr(1)]}


def ch(src, dst, status="Open"):
    return {"source": src, "destination": dst, "status": status}


def exact(topo):
    return [ch(c["address"], c["relay_address"]) for c in topo["clients"]] + \
           [ch(c["exit_address"], c["relay_address"]) for c in topo["clients"]]


def test_exact_topology_passes():
    topo = relaytopo.with_addresses(relaytopo.layout("paired", 2), status(4, 2))
    problems, summary = relaytopo.check_channels(topo, exact(topo))
    assert problems == []
    assert summary["gnosis_vpn-client-2"] == "node-1 Open"


def test_shared_exits_all_to_the_one_relay():
    topo = relaytopo.with_addresses(relaytopo.layout("shared", 3), status(4, 3))
    assert relaytopo.check_channels(topo, exact(topo))[0] == []


@pytest.mark.parametrize("mutate, word", [
    (lambda t, chs: chs + [ch(t["clients"][0]["address"], t["clients"][1]["relay_address"])], "2 channels out"),
    (lambda t, chs: [c for c in chs if c["source"] != t["clients"][1]["exit_address"]], "0 channels out"),
    (lambda t, chs: [dict(c, destination=t["clients"][1]["relay_address"]) if c["source"] == t["clients"][0]["address"] else c for c in chs], "want node-0"),
    (lambda t, chs: [dict(c, status="PendingToClose") if c["source"] == t["clients"][0]["address"] else c for c in chs], "want Open"),
])
def test_deviations_are_named(mutate, word):
    topo = relaytopo.with_addresses(relaytopo.layout("paired", 2), status(4, 2))
    problems, _ = relaytopo.check_channels(topo, mutate(topo, exact(topo)))
    assert problems and any(word in p for p in problems), problems


def test_closed_channels_do_not_count():
    topo = relaytopo.with_addresses(relaytopo.layout("paired", 1), status(2, 1))
    chs = exact(topo) + [ch(topo["clients"][0]["address"], addr(7), "Closed")]
    assert relaytopo.check_channels(topo, chs)[0] == []


def running(st):
    return dict(st, state="running")


def test_stale_reason_matches_the_live_cluster():
    st = running(status(4, 2))
    topo = relaytopo.with_addresses(relaytopo.layout("paired", 2), st)
    assert relaytopo.stale_reason(topo, st) == ""


def test_stale_reason_names_a_later_cluster_or_none():
    topo = relaytopo.with_addresses(relaytopo.layout("shared", 3), running(status(4, 3)))
    later = running(status(4, 3))
    later["nodes"][2]["address"] = addr(55)
    assert "node-2" in relaytopo.stale_reason(topo, later)
    assert "3 nodes" in relaytopo.stale_reason(topo, running(status(3, 3)))      # the standard stack after `just up-nobuild`
    assert relaytopo.stale_reason(topo, None) == "no running localcluster"


def test_attribution_floor_rounds_up():
    from suitelib.relaybench import attribution_floor
    assert attribution_floor(25_000_000, 1000) == 25_000
    assert attribution_floor(1001, 1000) == 2


def test_parse_cpu_sample():
    from suitelib.hosts import parse_cpu_sample
    lines = ["100", "cpu  10 0 10 70 10 0 0 0 0 0",
             "42 42 (hoprd x) S 1 2 3 4 5 6 7 8 9 10 250 50 0 0",
             "43 "]
    busy, total, per = parse_cpu_sample(lines)
    assert (busy, total) == (20, 100)
    assert per == {42: 3.0, 43: None}
    assert parse_cpu_sample([]) == (None, None, {})


def test_cpu_groups_single_and_multi_machine():
    from suitelib.config import Config
    from suitelib.relaybench import cpu_groups, cpu_text
    topo = relaytopo.layout("paired", 2)
    single = {"nodes": [{"id": i, "pid": 10 + i} for i in range(4)]}
    g = cpu_groups(Config({}), topo, single)
    assert list(g) == ["local"] and g["local"][1] == "host" and g["local"][2] == {"node-0": 10, "node-1": 11, "node-2": 12, "node-3": 13}
    multi = {"multihost": True, "nodes": [{"id": 0, "pid": 10, "ssh": "root@r1"}, {"id": 1, "pid": 11, "ssh": "root@r2"},
                                          {"id": 2, "pid": 12, "ssh": "root@e1"}, {"id": 3, "pid": 13, "ssh": "root@e1"}],
             "clients": {"gnosis_vpn-client": {"ssh": "root@c1"}, "gnosis_vpn-client-2": {"ssh": "root@c2"}}}
    g = cpu_groups(Config({}), topo, multi)
    assert {k: v[1] for k, v in g.items()} == {"root@r1": "relays", "root@r2": "relays", "root@e1": "exits", "root@c1": "clients", "root@c2": "clients"}
    assert g["root@e1"][2] == {"node-2": 12, "node-3": 13} and not g["root@c1"][0].local
    shared = {"multihost": True, "nodes": [{"id": i, "pid": i, "ssh": "root@x"} for i in range(4)], "clients": {"gnosis_vpn-client": {"ssh": None}}}
    g = cpu_groups(Config({}), topo, shared)
    assert {k: v[1] for k, v in g.items()} == {"root@x": "exits+relays", "local": "clients"}
    assert cpu_text({"host": 93.1}) == "93.1 %"
    assert cpu_text({"relays": 97.2, "clients": 40.0}) == "clients 40.0 % / relays 97.2 %"
    assert cpu_text({"relays": 60.1}, {"relays": 71.0}) == "relays 60.1 % (max 71.0)"


def test_multihost_merge_spread_and_client_docker(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import multihost
    from suitelib.client import client_docker_host
    from suitelib.config import Config
    from suitelib.hosts import Host
    assert [k for _, k in multihost.spread(5, ["a", "b"])] == [3, 2]
    assert [m for m, _ in multihost.spread(1, ["a", "b", "c"])] == ["a"]
    roles = {"chain": {"machines": [{"name": "control", "ssh": None, "addr": "10.0.0.1", "host": Host(None)}]}}
    r1 = {"name": "relay-1", "ssh": "root@10.0.0.2", "addr": "10.0.0.2"}
    e1 = {"name": "exit-1", "ssh": "root@10.0.0.3", "addr": "10.0.0.3"}
    for r in ("relays", "exits", "clients"):
        roles[r] = {"machines": []}
    started = [("relays", r1, {"nodes": [{"id": 0, "address": addr(1), "api_url": "http://10.0.0.2:3000"}], "extras": [{"id": 0}]}),
               ("exits", e1, {"nodes": [{"id": 0, "address": addr(3), "api_url": "http://10.0.0.3:3100/"}]})]
    m = multihost.merge(roles, "-i k", started)
    assert [(n["id"], n["role"], n["machine"], n["api_url"]) for n in m["nodes"]] == [
        (0, "relays", "relay-1", "http://10.0.0.2:3000"), (1, "exits", "exit-1", "http://10.0.0.3:3100")]
    assert m["blokli_url"] == "http://10.0.0.1:8080" and m["extras_ssh"] == "root@10.0.0.2"
    assert [(x["id"], x["ssh"]) for x in m["extras"]] == [(0, "root@10.0.0.2")]
    two = multihost.merge(roles, "", [("relays", r1, {"nodes": [], "extras": [{"id": 0, "address": addr(50)}, {"id": 1, "address": addr(51)}]}),
                                      ("exits", e1, {"nodes": [], "extras": [{"id": 0, "address": addr(52)}]})])
    assert [(x["id"], x["cluster_id"], x["ssh"]) for x in two["extras"]] == [(0, 0, "root@10.0.0.2"), (1, 1, "root@10.0.0.2"), (2, 0, "root@10.0.0.3")]
    m["clients"] = {"gnosis_vpn-client": {"ssh": "root@10.0.0.9"}, "gnosis_vpn-client-2": {"ssh": None}}
    f = tmp_path / "mh.json"
    f.write_text(json.dumps(m))
    cfg = Config({"MULTIHOST_STATUS": str(f)})
    assert client_docker_host(cfg, "gnosis_vpn-client") == "ssh://root@10.0.0.9"
    assert client_docker_host(cfg, "gnosis_vpn-client-2") == ""
    assert client_docker_host(Config({}), "gnosis_vpn-client") == ""


def test_multihost_need_reads_only_its_role():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import multihost
    assert multihost.need("chain", {}) == ([], [multihost.CHAIN_IMAGE], False)
    assert multihost.need("relays", {"hoprd_bin": "/h", "localcluster_bin": "/l"}) == (["/h", "/l"], [], False)
    assert multihost.need("clients", {})[2] is True


def test_single_exit_layout_and_channels():
    lay = relaytopo.layout("single-exit", 3)
    assert lay["relays"] == [0, 1, 2] and lay["exits"] == [3] and lay["cluster_size"] == 4
    # one exit, one VPN server: every client's config points at server 0 (a server per client hid the server's share)
    assert lay["servers"] == 1 and {c["server"] for c in lay["clients"]} == {0}
    assert relaytopo.layout("paired", 3)["servers"] == 3 and relaytopo.layout("shared", 3)["servers"] == 3
    assert [c["server"] for c in relaytopo.layout("shared", 3)["clients"]] == [0, 1, 2]
    old = relaytopo.layout("single-exit", 3, server_per_client=True)
    assert old["servers"] == 3 and [c["server"] for c in old["clients"]] == [0, 1, 2]
    cfg = tomllib.loads(relaytopo.render_client_config(TEMPLATES, relaytopo.with_addresses(lay, status(4, 3))["clients"][2]))
    assert cfg["connection"]["bridge"]["target"] == "127.0.0.1:8000" and cfg["connection"]["wg"]["target"] == "127.0.0.1:51821"
    assert [(c["relay"], c["exit"], c["dest"]) for c in lay["clients"]] == [(0, 3, "node-3"), (1, 3, "node-3"), (2, 3, "node-3")]
    topo = relaytopo.with_addresses(lay, status(4, 3))
    ok = [ch(c["address"], c["relay_address"]) for c in topo["clients"]] + [ch(addr(3), addr(r)) for r in (0, 1, 2)]
    assert relaytopo.check_channels(topo, ok)[0] == []
    one_short = ok[:-1]
    assert any("2 channels out" in p and "want 3" in p for p in relaytopo.check_channels(topo, one_short)[0])
    wrong = ok[:-1] + [ch(addr(3), addr(9))]
    assert any("want node-0, node-1, node-2" in p for p in relaytopo.check_channels(topo, wrong)[0])


def test_overlap_rates_use_the_window_all_transfers_share():
    from suitelib.relaybench import interp, overlap
    # client A moves 10 MB/s from t=0 to t=10, client B moves 5 MB/s from t=2 to t=12
    a = {"start": 0.0, "first": 0.0, "last": 10.0, "samples": [[0.0, 0], [10.0, 100_000_000]]}
    b = {"start": 0.5, "first": 2.0, "last": 12.0, "samples": [[2.0, 0], [12.0, 50_000_000]]}
    assert interp(a["samples"], 5.0) == 50_000_000 and interp(b["samples"], 1.0) == 0 and interp(b["samples"], 20) == 50_000_000
    o = overlap([a, b])
    assert o["window"] == [2.0, 10.0] and o["window_s"] == 8.0 and o["start_skew_s"] == 0.5
    assert o["per_client_mbit"] == [80.0, 40.0] and o["agg_mbit"] == 120.0 and o["min_mbit"] == 40.0
    single = overlap([a])
    assert single["agg_mbit"] == 80.0 and single["window_s"] == 10.0
    dead = overlap([a, {"start": 0.1, "first": None, "last": None, "samples": []}])
    assert dead["agg_mbit"] == 0 and dead["window"] is None and dead["buckets"] == []


def test_buckets_and_stalls_show_what_the_mean_hides():
    from suitelib.relaybench import bucket_rates, longest_stall, overlap
    # 10 MB/s for 10 s, nothing for 10 s, 10 MB/s for 10 s: the mean says 53 Mbit/s, the buckets say 80 / 0 / 80
    r = {"start": 0.0, "first": 0.0, "last": 30.0,
         "samples": [[0.0, 0], [10.0, 100_000_000], [20.0, 100_000_000], [20.5, 105_000_000], [30.0, 200_000_000]]}
    assert bucket_rates([r], 0.0, 30.0, 10) == [80.0, 0.0, 80.0]
    assert bucket_rates([r], 0.0, 25.0, 10) == [80.0, 0.0]            # the partial last bucket is dropped
    assert bucket_rates([r], 0.0, 4.0, 10) == [80.0]                  # a window shorter than a bucket is one bucket
    assert longest_stall(r) == 10.5                                   # from the last byte before the gap to the first after it
    assert longest_stall({"first": None, "samples": []}) is None
    o = overlap([r], bucket_s=10)
    assert o["agg_mbit"] == 53.333 and (o["bucket_min"], o["bucket_median"], o["bucket_max"]) == (0.0, 80.0, 80.0) and o["stall_max_s"] == 10.5


def test_tunnel_shortfall_needs_the_bytes_on_the_interface():
    from suitelib.relaybench import tunnel_shortfall
    ok = {"dir": "down", "bytes": 1000, "tunnel": {"iface": "wg0", "rx": 1100, "tx": 40, "recreated": False}}
    assert tunnel_shortfall(ok) is None
    assert tunnel_shortfall(dict(ok, dir="up")).startswith("only 40 B crossed wg0")         # an upload is held against tx
    assert "no tunnel interface" in tunnel_shortfall({"dir": "down", "bytes": 1000, "tunnel": None})
    assert "recreated" in tunnel_shortfall({"dir": "down", "bytes": 1, "tunnel": {"iface": "wg0", "rx": None, "tx": None, "recreated": True}})


SAMPLER_LINES = """K 100
T 100.0
C cpu  100 0 100 800 0 0 0 0 0 0
P 42 42 (hoprd x) S 1 2 3 4 5 6 7 8 9 10 250 50 0 0
N Inter-|   Receive                                                |  Transmit
N  face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
N     lo: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0
N   eth1: 1000 1 0 0 0 0 0 0 2000 1 0 0 0 0 0 0
N veth9a: 7 1 0 0 0 0 0 0 7 1 0 0 0 0 0 0
U Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors IgnoredMulti MemErrors
U Udp: 10 0 1 10 1 0 0 0 0
E
T 110.0
C cpu  500 0 100 1400 0 0 0 0 0 0
P 42 42 (hoprd x) S 1 2 3 4 5 6 7 8 9 10 1250 50 0 0
N     lo: 9 1 0 0 0 0 0 0 9 1 0 0 0 0 0 0
N   eth1: 11000 1 0 0 0 0 0 0 42000 1 0 0 0 0 0 0
U Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors IgnoredMulti MemErrors
U Udp: 90 0 7 90 7 0 0 0 0
E
T 120.0
C cpu  500 0""".splitlines()


def test_machine_samples_and_window():
    from suitelib.hosts import machine_window, parse_machine_samples, wire_bytes
    smp = parse_machine_samples(SAMPLER_LINES)
    assert [x["t"] for x in smp] == [100.0, 110.0]                   # the sample cut short at the end is dropped
    assert (smp[0]["busy"], smp[0]["total"]) == (200, 1000) and smp[0]["pid"] == {42: 3.0}
    assert wire_bytes(smp[0]) == (1000, 2000)                        # lo and veth are not the wire
    w = machine_window(smp, 100.0, 110.0, [42, 43])
    # 400 of 1000 jiffies busy; the process 10 cpu-seconds in 10 s; 10 kB in and 40 kB out; six receive-buffer drops
    assert w["cpu_pct"] == 40.0 and w["pid_pct"] == {42: 100.0, 43: None} and (w["wire_rx"], w["wire_tx"]) == (10000, 40000)
    assert w["udp_errors"] == {"InErrors": 6, "RcvbufErrors": 6, "SndbufErrors": 0, "MemErrors": 0} and w["covered_s"] == 10.0
    half = machine_window(smp, 105.0, 200.0, [42])                   # clipped to what the samples cover, interpolated
    assert half["covered_s"] == 5.0 and half["cpu_pct"] == 40.0 and half["wire_tx"] == 20000
    assert machine_window(smp[:1], 0, 1e12)["cpu_pct"] is None and machine_window(smp, 300, 400)["covered_s"] == 0.0


def test_sampler_runs_the_loop_on_this_machine_and_leaves_nothing_behind():
    import os
    import subprocess
    import time
    from suitelib.hosts import Host, Sampler
    smp = Sampler(Host(None), [os.getpid(), 0], interval=0.2, max_s=30).start()
    assert smp.wait_first(10)
    time.sleep(0.6)
    got = smp.stop()
    assert len(got) >= 2 and got[0]["total"] and os.getpid() in got[0]["pid"] and "lo" in got[0]["net"]
    assert got[-1]["t"] > got[0]["t"] and abs(got[0]["t"] - time.time()) < 60
    time.sleep(0.5)
    assert subprocess.run(["pgrep", "-f", "printf 'K %s"], capture_output=True).returncode == 1      # the loop died with its reader


def test_window_summarises_machines_per_role():
    from suitelib.hosts import Host, parse_machine_samples
    from suitelib.relaybench import MachineWatch, counts_delta
    groups = {"root@e1": (Host(None), "exits", {"node-2": 42}), "root@c1": (Host(None), "clients", {})}
    watch = MachineWatch(groups, 1, 30)
    watch.samplers["root@e1"].samples = parse_machine_samples(SAMPLER_LINES)
    w = watch.window(100.0, 110.0)
    assert w.host_pct == {"exits": 40.0} and w.node_pct == {"node-2": 100.0} and w.machine_pct == {"root@e1": 40.0, "root@c1": None}
    assert w.wire == {"exits": (10000, 40000)} and w.udp["exits"]["RcvbufErrors"] == 6 and w.covered_s == 0.0
    whole = watch.window()
    assert whole.wire == {"exits": (10000, 40000)}
    d = counts_delta({3: {"sent": 10, "received": None}}, {3: {"sent": 25, "received": 4}})
    assert d == {3: {"sent": 15, "received": None}}


def test_transferprobe_download_and_upload_against_the_target():
    import subprocess
    import threading
    tests = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(tests.parent / "docker" / "target"))
    import speedtarget
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), speedtarget.Handler)
    speedtarget.CHUNK = speedtarget.make_chunk(speedtarget.DEFAULT_SEED)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        for d in ("down", "up"):
            r = subprocess.run([sys.executable, str(tests / "probes" / "transferprobe.py"), "--host", "127.0.0.1", "--port", str(port),
                                "--dir", d, "--bytes", "3000000", "--tick", "0.01"], capture_output=True, text=True, timeout=60)
            out = json.loads(r.stdout)
            assert out["complete"] and out["bytes"] == 3000000 and out["code"] == 200, out
            assert out["samples"][-1][1] == 3000000 and out["first"] <= out["last"] <= out["done"]
            # an upload's log counts what the target's TCP acknowledged, not what was handed to the socket
            assert out["counted"] == ("read" if d == "down" else "acked") and out["acked"] == (None if d == "down" else 3000000)
            assert out["tunnel"] is None
        r = subprocess.run([sys.executable, str(tests / "probes" / "transferprobe.py"), "--host", "127.0.0.1", "--port", str(port),
                            "--dir", "down", "--bytes", "3000000", "--iface", "lo"], capture_output=True, text=True, timeout=60)
        t = json.loads(r.stdout)["tunnel"]
        assert t["iface"] == "lo" and t["rx"] >= 3000000 and t["tx"] >= 3000000 and not t["recreated"], t
    finally:
        srv.shutdown()



def test_multihost_refuses_duplicate_client_identities():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import multihost
    r1 = {"name": "relay-1", "ssh": "root@a", "addr": "10.0.0.2"}
    e1 = {"name": "exit-1", "ssh": "root@b", "addr": "10.0.0.3"}
    roles = {"chain": {"machines": [{"name": "c", "ssh": None, "addr": "10.0.0.1"}]}, "relays": {"machines": []},
             "exits": {"machines": []}, "clients": {"machines": []}}
    frozen = [{"id": 0, "address": addr(60)}, {"id": 1, "address": addr(61)}]
    with pytest.raises(SystemExit, match="are the same"):
        multihost.merge(roles, "", [("relays", r1, {"nodes": [], "extras": frozen}), ("exits", e1, {"nodes": [], "extras": frozen})])
