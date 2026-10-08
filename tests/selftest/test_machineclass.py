"""Offline checks for the relay machine classes of T36-relay-machine-class: the table in tests/machine_classes.toml, the
machine probe, the threshold rule (pytest tests/selftest, just suite-selftest)."""
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from suitelib import machineclass  # noqa: E402
from suitelib.stats import ping_summary  # noqa: E402

C4_PROBE = "4\nIntel(R) Xeon(R) Platinum 8168 CPU @ 2.70GHz\n8131784\nprovider digitalocean\n"


@pytest.fixture(scope="module")
def table():
    return machineclass.load()


def test_table_reads_and_renders_back_unchanged(table):
    assert tomllib.loads(machineclass.render(table)) == table


def test_every_ladder_names_classes_of_the_table(table):
    for provider, pv in table["providers"].items():
        for lad, sizes in pv["ladders"].items():
            assert machineclass.ladder(table, f"{provider}:{lad}") == [f"{provider}:{s}" for s in sizes]
            for s in sizes:
                machineclass.lookup(table, f"{provider}:{s}")


def test_thresholds_follow_the_rule_from_the_measurements(table):
    cal = table["calibration"]
    for name in machineclass.known(table):
        entry = machineclass.lookup(table, name)[2]
        if "measured_agg_mbit" not in entry:
            continue
        agg, ping = machineclass.derive(entry["measured_agg_mbit"], entry["measured_ping_p50_ms"], cal["floor_pct"], cal["ping_mult"])
        assert (entry["agg_min_mbit"], entry["ping_p50_max_ms"]) == (agg, ping), name
        assert all(0 < a < m for a, m in zip(agg, min(entry["measured_agg_mbit"], key=len))), name


def test_derive_takes_the_worst_run_and_cuts_at_the_shortest():
    agg, ping = machineclass.derive([[10.0, 20.0, 30.0], [9.0, 22.0]], [[400.0, 900.0, 1500.0], [450.0, 1000.0]], 75, 2)
    assert agg == [6.5, 15.0]            # 75 % of 9.0 = 6.75 -> 6.5; of 20.0 = 15.0
    assert ping == [900, 2000]           # 2 x 450 = 900; 2 x 1000 = 2000


def test_unknown_class_and_ladder_name_what_is_known(table):
    with pytest.raises(KeyError, match="digitalocean:c-4"):
        machineclass.lookup(table, "digitalocean:c-3")
    with pytest.raises(KeyError, match="cpu-optimized"):
        machineclass.ladder(table, "digitalocean:nope")


def test_probe_parses_and_matches_its_class(table):
    seen = machineclass.parse_probe(C4_PROBE)
    assert seen == {"vcpus": 4, "cpu_model": "Intel(R) Xeon(R) Platinum 8168 CPU @ 2.70GHz", "mem_kb": 8131784, "providers": ["digitalocean"]}
    provider, _, entry = machineclass.lookup(table, "digitalocean:c-4")
    assert machineclass.mismatch(provider, entry, seen) == ([], [])
    # the basic 4-vCPU size has the same vCPUs and memory; only the CPU model tells them apart
    provider, _, entry = machineclass.lookup(table, "digitalocean:s-4vcpu-8gb")
    hard, soft = machineclass.mismatch(provider, entry, seen)
    assert not hard and soft
    assert machineclass.parse_probe("") is None and machineclass.parse_probe("ssh: connect refused") is None


def test_probe_mismatches_are_named(table):
    provider, _, entry = machineclass.lookup(table, "digitalocean:c-8")
    hard, _ = machineclass.mismatch(provider, entry, machineclass.parse_probe("4\nXeon\n8131784\n"))
    assert len(hard) == 3            # no provider, 4 vCPUs, half the memory
    assert "not on digitalocean" in hard[0]


def test_probe_command_asks_every_provider(table):
    cmd = machineclass.probe_cmd(table)
    assert "169.254.169.254" in cmd and "echo provider digitalocean" in cmd and cmd.endswith("; true")


def test_rungs_past_the_lists_are_uncalibrated(table):
    entry = machineclass.lookup(table, "digitalocean:s-8vcpu-16gb")[2]
    assert machineclass.thresholds(entry, 3) == (entry["agg_min_mbit"][2], entry["ping_p50_max_ms"][2])
    assert machineclass.thresholds(entry, 4) == (None, None)


def test_knob_drift_names_the_changed_knob(table):
    knobs = dict(table["calibration"]["knobs"])
    assert machineclass.knob_drift(table, knobs) == []
    knobs["CAP"] = 180
    assert machineclass.knob_drift(table, knobs) == [("CAP", 300, 180)]


def test_ping_summary_pools_the_transfers_and_counts_lost_echoes():
    res = [{"pings": [[1, 0.0, 100.0], [2, 1.0, None], [3, 2.0, 300.0]]}, {"pings": [[1, 0.0, 200.0]]}]
    assert ping_summary(res) == {"sent": 4, "lost": 1, "p50_ms": 200.0, "p95_ms": 300.0, "loss_pct": 25.0}
    assert ping_summary([{"bytes": 5}]) is None
