"""Relay-scaling topologies for T33-relay-baseline and T34-single-relay-scaling.

A topology pins every client to one relay and one exit through the channel graph alone. The client config has
no fixed intermediates (v6 takes only a hop count), so at one hop the path is decided by the channels:

- the client holds exactly one outgoing channel, to its relay (the client's own strategy opens it: allowlist of
  that one relay, min = target = 1 channel), so the forward path is client -> relay -> exit;
- the exit holds exactly one outgoing channel, to the same relay (opened here through its REST API), so every
  return path the client can build is exit -> relay -> client;
- no other node has a channel. The final hop of a path needs none (RFC-0010 edges, RFC-0014 phase-2 fallback), so
  the relay forwards in both directions without one.

Two layouts on one localcluster started with `--channel-management none`:

- paired N (T33-relay-baseline): relays are nodes 0..N-1, exits N..2N-1; client k uses relay k-1 and exit N+k-1.
- shared N (T34-single-relay-scaling): node 0 is the only relay, exits are 1..N; client k uses exit k.

Client k also has its own VPN server (gnosis_vpn-server-{k-1}, bridge 8000+k-1, WireGuard 51821+k-1), so N exits
means N hoprd exit nodes with N servers behind them; one traffic target serves all. The layout is written to
CONFIG_DIR/relay-topology.json by tests/relay_topology.py and read back by the tests."""
import json
import string
from pathlib import Path

MODES = ("paired", "shared")
TOPOLOGY_FILE = "relay-topology.json"


def client_name(k, base="gnosis_vpn-client"):
    """The suite's container names: the primary is gnosis_vpn-client, the others gnosis_vpn-client-<k>."""
    return base if k == 1 else f"{base}-{k}"


def layout(mode, n):
    """Node indices per role and the client -> (relay, exit, server) assignment, before any address is known."""
    if mode not in MODES:
        raise ValueError(f"mode {mode!r}: one of {', '.join(MODES)}")
    n = int(n)
    if n < 1:
        raise ValueError(f"n={n}: at least one client")
    if mode == "paired":
        relays, exits = list(range(n)), list(range(n, 2 * n))
        pick_relay = lambda k: relays[k - 1]           # noqa: E731
    else:
        relays, exits = [0], list(range(1, n + 1))
        pick_relay = lambda k: 0                       # noqa: E731
    clients = [{"k": k, "name": client_name(k), "extra": k - 1, "relay": pick_relay(k), "exit": exits[k - 1],
                "server": k - 1, "dest": f"node-{exits[k - 1]}", "config": f"client-{k}.toml"} for k in range(1, n + 1)]
    return {"mode": mode, "n": n, "cluster_size": len(relays) + len(exits), "relays": relays, "exits": exits, "clients": clients}


def with_addresses(lay, status):
    """Fill in the chain addresses from the localcluster status JSON (nodes[i].address, extras[i].address)."""
    nodes = {nd["id"]: nd for nd in status.get("nodes", [])}
    extras = {ex["id"]: ex for ex in status.get("extras", [])}
    out = json.loads(json.dumps(lay))
    out["node_address"] = {str(i): nodes[i]["address"] for i in lay["relays"] + lay["exits"]}
    for c in out["clients"]:
        c["address"] = extras[c["extra"]]["address"]
        c["relay_address"] = nodes[c["relay"]]["address"]
        c["exit_address"] = nodes[c["exit"]]["address"]
    return out


def render_client_config(templates_dir, c, hops=1, pix_on=False):
    """client-<k>.toml: the testenv client template with one destination (this client's exit), this client's own
    server ports, and a channel strategy that opens exactly one channel, to this client's relay."""
    t = Path(templates_dir)
    dest = string.Template((t / "destination.toml.tpl").read_text()).substitute(
        DEST_ID=str(c["exit"]), DEST_ADDRESS=c["exit_address"], DEST_HOPS=str(hops))
    dest = dest.rstrip("\n") + f"\npath    = {{ hops = {int(hops)} }}\n"
    pix = (t / ("pix-on.toml.tpl" if pix_on else "pix-off.toml.tpl")).read_text()
    text = string.Template((t / "client.toml.tpl").read_text()).safe_substitute(DESTINATIONS=dest, PIX_SECTION=pix)
    for old, new in (("127.0.0.1:8000", f"127.0.0.1:{8000 + c['server']}"), ("127.0.0.1:51821", f"127.0.0.1:{51821 + c['server']}")):
        if old not in text:
            raise ValueError(f"client template has no {old}; cannot point client {c['k']} at its own server")
        text = text.replace(old, new)
    text = text.rstrip("\n") + (
        "\n\n# relay-scaling topology: exactly one outgoing channel, to this client's relay\n"
        "[strategy]\nmin_open_channels = 1\ntarget_open_channels = 1\n\n"
        f"[strategy.channel_allowlist]\nenabled = true\npeers = [\"{c['relay_address']}\"]\n")
    return text


def save(config_dir, topo):
    p = Path(config_dir) / TOPOLOGY_FILE
    p.write_text(json.dumps(topo, indent=2) + "\n")
    return p


def load(config_dir):
    p = Path(config_dir) / TOPOLOGY_FILE
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _live(ch):
    return str(ch.get("status", "")).lower() != "closed"


def check_channels(topo, channels, clients=None):
    """Hold the channel graph against the topology. channels: the `all` list of GET /api/v4/channels?fullTopology=true
    (source, destination, status). For each client (all, or the given subset) and its exit: exactly one channel out
    that is not Closed, it is Open, and it goes to the assigned relay. Returns (problems, summary); problems is a list
    of one-line complaints, empty when the graph is exactly the topology. Relays' own channels are reported in the
    summary: none are needed and none are opened."""
    out_of = {}
    for ch in channels:
        if _live(ch):
            out_of.setdefault(str(ch.get("source", "")).lower(), []).append(ch)
    names = {a.lower(): f"node-{i}" for i, a in topo.get("node_address", {}).items()}
    for c in topo["clients"]:
        names[c["address"].lower()] = c["name"]

    def show(chs):
        return ", ".join(f"{names.get(str(x.get('destination', '')).lower(), x.get('destination'))} {x.get('status')}" for x in chs) or "none"

    problems, summary = [], {}
    wanted = [c for c in topo["clients"] if clients is None or c["k"] in clients]
    seen_exits = set()
    for c in wanted:
        relay = c["relay_address"].lower()
        ends = [(c["name"], c["address"])]
        if c["exit"] not in seen_exits:
            ends.append((f"node-{c['exit']} (exit of {c['name']})", c["exit_address"]))
            seen_exits.add(c["exit"])
        for who, addr in ends:
            chs = out_of.get(addr.lower(), [])
            summary[who] = show(chs)
            if len(chs) != 1:
                problems.append(f"{who}: {len(chs)} channels out ({show(chs)}), want exactly one, to node-{c['relay']}")
            elif str(chs[0].get("destination", "")).lower() != relay:
                problems.append(f"{who}: its channel goes to {show(chs)}, want node-{c['relay']}")
            elif str(chs[0].get("status", "")).lower() != "open":
                problems.append(f"{who}: its channel to node-{c['relay']} is {chs[0].get('status')}, want Open")
    for r in topo["relays"]:
        addr = topo.get("node_address", {}).get(str(r), "")
        summary[f"node-{r} (relay)"] = show(out_of.get(addr.lower(), []))
    return problems, summary
