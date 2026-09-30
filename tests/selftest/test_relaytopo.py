"""Offline checks for the relay-scaling topologies (suitelib/relaytopo.py): layout, per-client config, channel check."""
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
    topo = relaytopo.layout("paired", 1)
    single = {"nodes": [{"id": 0, "pid": 10}, {"id": 1, "pid": 11}]}
    g = cpu_groups(Config({}), topo, single)
    assert list(g) == ["host"] and g["host"][1] == {"node-0": 10, "node-1": 11}
    multi = {"nodes": [{"id": 0, "pid": 10, "ssh": "root@r"}, {"id": 1, "pid": 11, "ssh": "root@e"}]}
    g = cpu_groups(Config({}), topo, multi)
    assert sorted(g) == ["clients", "exits", "relays"]
    assert g["relays"][1] == {"node-0": 10} and g["clients"][1] == {}
    assert cpu_text({"host": 93.1}) == "93.1 %"
    assert cpu_text({"clients": 40.0, "relays": 97.2}) == "clients 40.0 % / relays 97.2 %"


def test_multihost_merge_gives_global_ids_and_remote_urls():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import multihost
    from suitelib.hosts import Host
    roles = {r: {"host": Host(None), "ssh": f"root@{r}", "addr": f"10.0.0.{i}"} for i, r in enumerate(multihost.ROLES)}
    st_r = {"nodes": [{"id": 0, "address": addr(1), "api_url": "http://10.0.0.1:3000"}, {"id": 1, "address": addr(2), "api_url": "http://10.0.0.1:3001"}],
            "extras": [{"id": 0}]}
    st_e = {"nodes": [{"id": 0, "address": addr(3), "api_url": "http://127.0.0.1:3100/"}]}
    m = multihost.merge(roles, "-i k", st_r, st_e, 2)
    assert [(n["id"], n["role"], n["cluster_id"], n["api_url"]) for n in m["nodes"]] == [
        (0, "relays", 0, "http://10.0.0.1:3000"), (1, "relays", 1, "http://10.0.0.1:3001"), (2, "exits", 0, "http://10.0.0.2:3100")]
    assert m["blokli_url"] == "http://10.0.0.0:8080" and m["extras"] == [{"id": 0}]
