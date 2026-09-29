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
