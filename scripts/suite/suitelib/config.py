"""Knobs. Every knob is an environment variable with a default; --fast selects the shorter of two defaults,
--very-fast additionally applies the VERY_FAST per-test overrides. A per-test knob is T<NN>_<VAR>
(environment or --knob) and beats --very-fast. Precedence, highest first:
  --knob T<NN>_<VAR>=v  >  env T<NN>_<VAR>  >  --very-fast override  >  default (fast or normal)."""
import os
from pathlib import Path


class Q(tuple):
    """Q(normal, fast): a default that changes with --fast."""
    __slots__ = ()

    def __new__(cls, normal, fast):
        return tuple.__new__(cls, (normal, fast))


def q(normal, fast):
    return Q(normal, fast)


# --very-fast: every long step cut to the shortest setting that still exercises its mechanism.
# A --very-fast run cannot see anything that needs a long session (its arms are 15 s); use --fast for that.
VERY_FAST = {
    "BYTES": 2000000, "CAP": 30, "REPS": 1,
    "T03_N": 3,
    "T04_SIZES_MIB": "1 2",
    "T05_PHASE_S": 8, "T05_PARALLEL": "1 3",
    "T06_ECHO_DUR": 15, "T06_STREAM_DUR": 15,
    "T09_RUNGS": "0 25", "T09_STREAM_S": 8,
    # the window after the kill must exceed RECOVER_MAX: a removed peer is noticed only by the liveness ping, ~75 s
    "T10_DUR": 130, "T10_REPEATS": 1, "T10_T_KILL": 20,
    "T11_CALL_S": 8, "T11_POLL_N": 4,
    "T12_UPSTREAMS": "16", "T12_PASSES": 1,
    "T13_MTUS": "1420 940", "T13_STREAM_S": 10,
    "T15_DELAYS": "0 5",
    "T18_LADDER": "4 8", "T18_STEP_S": 12,
    "T19_TRICKLES": "100",
    "T20_LOSSES": "5", "T20_STEP_S": 12,
    "T21_DUR": 20,
    "T22_LADDER": "1 2 4", "T22_CAP": 45,
    "T23_DUR": 60, "T23_INTERVAL": 30,
    "T24_DUR": 25,
}


def _coerce(value, like):
    """Parse a string knob value into the type of its default."""
    if isinstance(like, bool):
        return str(value).lower() in ("1", "true", "yes")
    if isinstance(like, int):
        return int(float(value))
    if isinstance(like, float):
        return float(value)
    return value


class Config:
    def __init__(self, env=None, fast=False, very_fast=False, knobs=None):
        self.env = dict(os.environ if env is None else env)
        self.very_fast = very_fast
        self.fast = fast or very_fast
        self.cli_knobs = dict(knobs or {})
        self.consumed = set()
        e = self.env
        here = Path(__file__).resolve().parent.parent
        self.suite_dir = here
        self.testenv_dir = Path(e.get("TESTENV_DIR") or here.parent.parent)
        self.client = e.get("CLIENT", "gnosis_vpn-client")
        self.client2 = e.get("CLIENT2", "gnosis_vpn-client-2")
        self.client_count = int(e.get("CLIENT_COUNT", "1"))
        # seconds after connect before measuring; see Client.connect for why this must not be raised
        self.surb_ramp_wait = int(e.get("SURB_RAMP_WAIT", "25"))
        self.dest = e.get("DEST", "node-0")
        self.target_name = e.get("TARGET_NAME", "gnosis_vpn-target")
        self.target_network = e.get("TARGET_NETWORK", "gnosis-vpn-target")
        self.docker_network = e.get("DOCKER_NETWORK", "gnosis-vpn-testenv")
        self.data_dir = Path(e.get("DATA_DIR", "/tmp/hopr-nodes"))
        self.config_dir = Path(e.get("CONFIG_DIR", "/tmp/gnosis_vpn-testenv"))
        self.cluster_size = int(e.get("CLUSTER_SIZE", "3"))
        hoprd_dir = e.get("HOPRD_DIR") or str(self.testenv_dir.parent / "hoprd")
        self.localcluster_bin = e.get("LOCALCLUSTER_BIN") or f"{hoprd_dir}/result-localcluster/bin/hoprd-localcluster"
        self.hoprd_bin = e.get("HOPRD_BIN") or f"{hoprd_dir}/result-hoprd/bin/hoprd"
        self.out_dir = Path(e.get("SUITE_OUT_DIR", "/tmp/gnosis_vpn-testenv-suite"))
        self.cell = e.get("SUITE_CELL", "")
        self.deadman = int(e.get("DEADMAN", "900"))
        self.connect_timeout = int(e.get("CONNECT_TIMEOUT", "240"))
        self.server = e.get("SERVER", "gnosis_vpn-server-0")
        self.netem_iface = e.get("NETEM_IFACE", "lo")
        self.save_log_raw = e.get("SAVE_LOG_RAW", "0") == "1"
        # production-network mode: an external target (TARGET_HOST, --target) replaces the in-cluster container and
        # NO_CLUSTER=1 (--no-cluster) tells the cluster-dependent checks there is no localcluster to ask
        self.target_host = e.get("TARGET_HOST", "")
        self.no_cluster = e.get("NO_CLUSTER", "0") == "1"
        # every test is killed after this many seconds unless its module computes its own TIMEOUT(knobs)
        self.test_timeout = int(e.get("TEST_TIMEOUT", "7200"))
        self.client_image = e.get("CLIENT_IMAGE", "")
        self.cluster_env = e.get("CLUSTER_ENV", "")
        self.cluster_latency = e.get("CLUSTER_LATENCY", "")
        self.client_extra_env = e.get("CLIENT_EXTRA_ENV", "")
        # transfer size and cap: a single-host localcluster moves a few Mbit/s, so 10 MB / 90 s completes where the
        # catalogue's field default (25 MB / 60 s against a public exit) would only measure the cap
        self.bytes = self.knob("", "BYTES", q(10000000, 5000000))
        self.cap = self.knob("", "CAP", q(90, 60))
        self.reps = self.knob("", "REPS", q(3, 2))

    def q(self, normal, fast):
        return fast if self.fast else normal

    def default(self, value):
        return self.q(*value) if isinstance(value, Q) else value

    def knob(self, prefix, name, default):
        """Resolve one knob. prefix is 'T23' for a per-test knob or '' for a global one."""
        dflt = self.default(default)
        key = f"{prefix}_{name}" if prefix else name
        self.consumed.add(key)
        for src in (self.cli_knobs, self.env):
            if key in src and src[key] != "":
                return _coerce(src[key], dflt)
        if self.very_fast and key in VERY_FAST:
            return _coerce(VERY_FAST[key], dflt)
        return dflt

    def unused_cli_knobs(self):
        """--knob names no test resolved; a typo would otherwise change nothing and say nothing."""
        return sorted(k for k in self.cli_knobs if k not in self.consumed)

    def knobs(self, prefix, spec):
        """Resolve a whole {NAME: default} spec into a Knobs object."""
        return Knobs({k: self.knob(prefix, k, v) for k, v in spec.items()})


class Knobs(dict):
    """Attribute access to resolved knobs: k.DUR."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)

    def words(self, k):
        """A space-separated knob ('0 25 50') as a list of strings."""
        return str(self[k]).split()

    def numbers(self, k):
        return [float(x) for x in self.words(k)]

    def describe(self):
        return " ".join(f"{k}={v}" for k, v in self.items())
