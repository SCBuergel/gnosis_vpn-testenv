"""Relay machine classes for T36-relay-machine-class: what one relay of a given provider and size carries in the
T34-single-relay-scaling shape, read from tests/machine_classes.toml.

A class is "<provider>:<size>" (digitalocean:c-4); a ladder is "<provider>:<ladder>" (digitalocean:cpu-optimized), the
provider's sizes of one series from small to large. Per class the table gives what the machine must look like (vcpus,
memory_gb, cpu_model: a substring of /proc/cpuinfo's model name), what the calibration sweep measured per rung
(measured_agg_mbit, measured_ping_p50_ms: the rung of n clients is entry n-1; one list per calibration run) and the
thresholds derived from it (agg_min_mbit, ping_p50_max_ms). A rung past the end of a class's threshold lists is
uncalibrated: recorded, not scored. The table's [calibration] says on what it was measured: the knobs, the versions,
the clients' and exits' machines; derive() is the one rule from measurement to threshold, and `machine_ladder.py
calibrate` rewrites the table with it."""
import json
import math
import shlex
import tomllib
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "machine_classes.toml"
# MemTotal is the RAM a size is sold with less what the kernel keeps: 0.956-0.979 of it on every 2026-10-07 droplet
MEM_RATIO = (0.90, 1.0)


def load(path=None):
    p = Path(path) if path else DEFAULT
    with open(p, "rb") as f:
        return tomllib.load(f)


def lookup(table, name):
    """(provider, size, entry) of class "provider:size"; KeyError naming the known classes."""
    provider, _, size = str(name).partition(":")
    sizes = table.get("providers", {}).get(provider, {}).get("sizes", {})
    if not size or size not in sizes:
        raise KeyError(f"no machine class {name!r} in the table; known: {', '.join(known(table))}")
    return provider, size, sizes[size]


def known(table):
    return [f"{p}:{s}" for p, v in table.get("providers", {}).items() for s in v.get("sizes", {})]


def ladder(table, name):
    """The classes of ladder "provider:ladder", small to large; KeyError naming the known ladders."""
    provider, _, lad = str(name).partition(":")
    ladders = table.get("providers", {}).get(provider, {}).get("ladders", {})
    if lad not in ladders:
        have = [f"{p}:{x}" for p, v in table.get("providers", {}).items() for x in v.get("ladders", {})]
        raise KeyError(f"no ladder {name!r} in the table; known: {', '.join(have)}")
    return [f"{provider}:{s}" for s in ladders[lad]]


def probe_cmd(table):
    """One shell line: vCPUs, CPU model, MemTotal kB, then `provider <name>` for every provider whose `detect` succeeds."""
    detect = " ; ".join(f"( {v['detect']} ) >/dev/null 2>&1 && echo provider {shlex.quote(p)}"
                        for p, v in table.get("providers", {}).items() if v.get("detect"))
    return ("nproc; grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | sed 's/^ *//'; awk '/^MemTotal/{print $2}' /proc/meminfo"
            + (f"; {detect}" if detect else "") + "; true")


def parse_probe(text):
    """{"vcpus", "cpu_model", "mem_kb", "providers"} from probe_cmd's output; None when the machine did not answer."""
    lines = [x.strip() for x in (text or "").splitlines()]
    if len(lines) < 3 or not lines[0].isdigit() or not lines[2].isdigit():
        return None
    return {"vcpus": int(lines[0]), "cpu_model": lines[1], "mem_kb": int(lines[2]),
            "providers": [x.split(" ", 1)[1] for x in lines[3:] if x.startswith("provider ")]}


def mismatch(provider, entry, seen):
    """Why a probed machine is not the class, as a list of reasons (empty: it is). The CPU model is listed apart
    (second list): a provider may move a size to new hardware, which changes the numbers but not the class."""
    hard, soft = [], []
    if provider not in seen["providers"]:
        hard.append(f"not on {provider} (its metadata service did not answer"
                    + (f"; detected: {', '.join(seen['providers'])})" if seen["providers"] else ")"))
    if seen["vcpus"] != int(entry["vcpus"]):
        hard.append(f"{seen['vcpus']} vCPUs, the class has {entry['vcpus']}")
    gb = seen["mem_kb"] / 1024 / 1024
    lo, hi = MEM_RATIO
    if not lo * float(entry["memory_gb"]) <= gb <= hi * float(entry["memory_gb"]):
        hard.append(f"{gb:.2f} GiB of MemTotal, the class has {entry['memory_gb']} GB")
    want = entry.get("cpu_model")
    if want and want not in seen["cpu_model"]:
        soft.append(f"CPU model {seen['cpu_model']!r}, calibrated on {want!r}")
    return hard, soft


def thresholds(entry, n):
    """(agg_min_mbit, ping_p50_max_ms) for the rung of n clients; None where that rung is uncalibrated."""
    agg, ping = entry.get("agg_min_mbit", []), entry.get("ping_p50_max_ms", [])
    return (agg[n - 1] if n <= len(agg) else None), (ping[n - 1] if n <= len(ping) else None)


def knob_drift(table, knobs):
    """The knobs that differ from the ones the table was calibrated with: [(name, calibrated, now)]."""
    cal = table.get("calibration", {}).get("knobs", {})
    return [(k, v, knobs.get(k)) for k, v in cal.items() if str(knobs.get(k)) != str(v)]


def derive(agg_runs, ping_runs, floor_pct, ping_mult):
    """The thresholds from the calibration runs of one class: per rung, floor_pct % of the lowest aggregate any run
    measured, rounded down to 0.5 Mbit/s, and ping_mult x the highest median round trip, rounded up to 100 ms. A rung
    counts as far as every run of the class measured it (a run that lost a rung cuts the lists there)."""
    depth = min(len(r) for r in agg_runs) if agg_runs else 0
    pdepth = min(len(r) for r in ping_runs) if ping_runs else 0
    agg = [math.floor(min(r[i] for r in agg_runs) * floor_pct / 100 * 2) / 2 for i in range(depth)]
    ping = [int(math.ceil(max(r[i] for r in ping_runs) * ping_mult / 100) * 100) for i in range(min(depth, pdepth))]
    return agg, ping


HEADER = """# Relay machine classes: T36-relay-machine-class's thresholds per infrastructure provider, machine size and rung.
# Generated by `python3 multihost/machine_ladder.py calibrate` from calibration runs; edit the ladders, providers and
# machine descriptions by hand, recalibrate the numbers. Format and rule: tests/suitelib/machineclass.py.
#
# measured_agg_mbit: aggregate download over the overlap, Mbit/s, one list per calibration run, entry n-1 = n clients
# measured_ping_p50_ms: median round trip through the tunnel during that download, ms
# agg_min_mbit, ping_p50_max_ms: the thresholds derived from them (derive(): floor_pct, ping_mult); a rung past the end
#   of these lists is uncalibrated and only recorded
"""


def _val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_val(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _key(k):
    return k if k.replace("-", "").replace("_", "").isalnum() else json.dumps(k)


def render(table):
    """The table as TOML text in the layout of tests/machine_classes.toml (tomllib reads it back unchanged)."""
    out = [HEADER, "[calibration]"]
    out += [f"{_key(k)} = {_val(v)}" for k, v in table.get("calibration", {}).items()]
    for provider, pv in table.get("providers", {}).items():
        out += ["", f"[providers.{_key(provider)}]"]
        out += [f"{_key(k)} = {_val(v)}" for k, v in pv.items() if k not in ("ladders", "sizes")]
        out += ["", f"[providers.{_key(provider)}.ladders]"]
        out += [f"{_key(k)} = {_val(v)}" for k, v in pv.get("ladders", {}).items()]
        for size, sv in pv.get("sizes", {}).items():
            out += ["", f"[providers.{_key(provider)}.sizes.{json.dumps(size)}]"]
            out += [f"{_key(k)} = {_val(v)}" for k, v in sv.items()]
    return "\n".join(out) + "\n"
