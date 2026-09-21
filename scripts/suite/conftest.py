"""pytest glue for the regression suite (docs/regression-catalogue.md).

THERE IS ONE RUN: `pytest` (via `just suite`) runs every gate and diagnostic in file order, t01 ... t24, every
time. Runbook items (t25 ... t32) are collected only with --runbook or when named with --only. Shorten a run
with --fast, --very-fast, --only/--skip, or a per-test knob (--knob T23_DUR=150 or the environment).

Results go to SUITE_OUT_DIR/<run-id>/ (rows.jsonl, verdicts.jsonl, summary.csv, console.log, run.txt, logs/,
samples/); a run id that already holds results is refused. T01 failing aborts the run."""
import os
import re
import signal
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from suitelib import client as clientlib  # noqa: E402
from suitelib.cluster import Cluster  # noqa: E402
from suitelib.config import Config  # noqa: E402
from suitelib.verdicts import KINDS, Checks, RunDir, log, write_summary  # noqa: E402
from suitelib.target import Target  # noqa: E402

TEST_FILE = re.compile(r"test_(t\d\d)_")


class _Tee:
    def __init__(self, stream):
        self.stream, self.file = stream, None

    def write(self, s):
        self.stream.write(s)
        if self.file:
            self.file.write(s)
            self.file.flush()
        return len(s)

    def flush(self):
        self.stream.flush()
        if self.file:
            self.file.flush()

    def isatty(self):
        return False

    def fileno(self):
        return self.stream.fileno()

    def __getattr__(self, k):
        return getattr(self.stream, k)


class SuiteState:
    def __init__(self, cfg):
        self.cfg = cfg
        self.run = None
        self.aborted = False
        self.tee_out = _Tee(sys.stdout)
        self.tee_err = _Tee(sys.stderr)
        self.started = None
        self.run_id = None
        self.known_knobs = {"BYTES", "CAP", "REPS"}     # every T<NN>_<VAR> is added at collection


def pytest_addoption(parser):
    g = parser.getgroup("suite", "gnosis_vpn regression suite")
    g.addoption("--fast", action="store_true", help="the catalogue's shorter durations")
    g.addoption("--very-fast", action="store_true", help="aggressive per-test overrides; cannot see anything that needs a long session")
    g.addoption("--knob", action="append", default=[], metavar="NAME=VALUE",
                help="override a knob (T23_DUR=150, BYTES=2000000); repeatable, beats the environment")
    g.addoption("--run-id", default=None, help="results directory name under SUITE_OUT_DIR (default: UTC stamp[-cell])")
    g.addoption("--cell", default=None, help="label of the version/config cell (SUITE_CELL)")
    g.addoption("--only", default="", metavar="tNN,tNN", help="run only these tests (t01 still runs first)")
    g.addoption("--skip", default="", metavar="tNN,tNN", help="drop these tests")
    g.addoption("--runbook", action="store_true", help="also collect the runbook items t25 ... t32")
    g.addoption("--no-preconditions", action="store_true", help="do not force t01 in front of an --only selection")
    g.addoption("--client", default=None, metavar="CONTAINER", help="the client container under test (CLIENT)")
    g.addoption("--dest", default=None, metavar="ID", help="the destination id in client.toml to connect to (DEST)")
    g.addoption("--target", default=None, metavar="HOST", help="an external traffic target running docker/target's services "
                "(TARGET_HOST): production-network runs, no in-cluster container")
    g.addoption("--no-cluster", action="store_true", help="no localcluster: cluster-dependent checks skip, node sampling is off")
    g.addoption("--timeout", type=int, default=None, metavar="S", help="per-test timeout in seconds (TEST_TIMEOUT); a module's "
                "TIMEOUT(knobs) overrides it")


def _is_live_arg(a):
    p = Path(a.split("::")[0])
    return "selftest" not in p.parts and (p.name == "tests" or TEST_FILE.search(p.name) is not None)


def _test_id(item):
    m = TEST_FILE.search(item.path.name)
    return m.group(1) if m else None


def pytest_configure(config):
    for k in KINDS:
        config.addinivalue_line("markers", f"{k}: test kind ({k})")
    knobs = {}
    for kv in config.getoption("--knob"):
        k, _, v = kv.partition("=")
        knobs[k] = v
    env = dict(os.environ)
    for opt, var in (("--cell", "SUITE_CELL"), ("--client", "CLIENT"), ("--dest", "DEST"), ("--target", "TARGET_HOST"), ("--timeout", "TEST_TIMEOUT")):
        if config.getoption(opt) is not None and config.getoption(opt) != "":
            env[var] = str(config.getoption(opt))
    if config.getoption("--no-cluster"):
        env["NO_CLUSTER"] = "1"
    cfg = Config(env, fast=config.getoption("--fast"), very_fast=config.getoption("--very-fast"), knobs=knobs)
    state = SuiteState(cfg)
    config._suite = state
    if getattr(config.option, "help", False) or getattr(config.option, "version", False) or config.option.collectonly:
        return
    # a junit.xml next to the other results, for CI and dashboards; the run directory is settled at collection
    if not config.option.xmlpath and any(_is_live_arg(a) for a in config.args):
        state.run_id = config.getoption("--run-id") or os.environ.get("SUITE_RUN_ID") \
            or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + (f"-{cfg.cell}" if cfg.cell else "")
        config.option.xmlpath = str(cfg.out_dir / state.run_id / "junit.xml")
    # the terminal reporter grabs sys.stdout right after this hook; the console log file is attached once the
    # run directory exists (pytest_collection_finish), so everything from the first test line on lands in it
    sys.stdout, sys.stderr = state.tee_out, state.tee_err


def pytest_collection_modifyitems(config, items):
    only = {x for x in config.getoption("--only").replace(",", " ").split() if x}
    skip = {x for x in config.getoption("--skip").replace(",", " ").split() if x}
    explicit = {Path(a.split("::")[0]).resolve() for a in config.invocation_params.args if not a.startswith("-")}
    keep, deselected = [], []
    state = config._suite
    for item in items:
        tid = _test_id(item)
        kind = getattr(item.module, "KIND", "gate")
        if tid:
            state.known_knobs.update(f"{tid.upper()}_{k}" for k in getattr(item.module, "KNOBS", {}))
        item.add_marker(getattr(pytest.mark, kind))
        named = tid in only or item.path.resolve() in explicit
        if tid is None:
            keep.append(item)                     # offline self-tests and anything not named test_tNN_*
            continue
        if kind == "runbook" and not (config.getoption("--runbook") or named):
            deselected.append(item)
            continue
        # preconditions run first even for --only: a leftover netem qdisc or a missing liveness-ping alias
        # poisons a single test as it poisons the run, and t01 costs five seconds
        if only and not named and not (tid == "t01" and not config.getoption("--no-preconditions")):
            deselected.append(item)
            continue
        if tid in skip:
            print(f"SKIP {tid}: --skip")
            deselected.append(item)
            continue
        keep.append(item)
    keep.sort(key=lambda i: (str(i.path), i.reportinfo()[1] or 0))
    items[:] = keep
    if deselected:
        config.hook.pytest_deselected(items=deselected)


def pytest_collection_finish(session):
    state = session.config._suite
    live = [i for i in session.items if _test_id(i)]
    if not live or session.config.option.collectonly:
        return
    cfg = state.cfg
    # a --knob no test declares changes nothing and would say nothing until the end; refuse it up front (every
    # module's KNOBS were registered at collection, selected or not, so --only does not hide a name)
    unknown = sorted(k for k in cfg.cli_knobs if k not in state.known_knobs)
    if unknown:
        raise pytest.UsageError(f"--knob {' '.join(unknown)}: no test declares these names; known: {' '.join(sorted(state.known_knobs))}")
    run_id = state.run_id or session.config.getoption("--run-id") or os.environ.get("SUITE_RUN_ID") \
        or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + (f"-{cfg.cell}" if cfg.cell else "")
    path = cfg.out_dir / run_id
    # A run id is a directory and every test appends to it. Reusing one blends two runs into one verdicts file
    # with no way to tell them apart afterwards. Refuse rather than merge.
    if (path / "verdicts.jsonl").exists() and (path / "verdicts.jsonl").stat().st_size > 0:
        raise pytest.UsageError(f"run id '{run_id}' already holds results in {path}; choose another --run-id or move that directory")
    state.run = RunDir(path, cfg.cell)
    latest = cfg.out_dir / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(state.run.path)
    except OSError:
        pass
    f = open(state.run / "console.log", "a")
    state.tee_out.file = f
    state.tee_err.file = f
    state.started = time.time()
    tests = " ".join(_test_id(i) for i in live)
    with open(state.run / "run.txt", "a") as r:
        r.write(f"run_id={run_id} cell={cfg.cell} very_fast={int(cfg.very_fast)} fast={int(cfg.fast)} "
                f"started={time.strftime('%FT%TZ', time.gmtime())}\ntests={tests}\n")
    print(f"run {run_id} -> {state.run.path}  tests={tests}")


def pytest_runtest_setup(item):
    state = item.config._suite
    if state.aborted:
        pytest.skip("preconditions failed - run aborted")
    tid = _test_id(item)
    if tid:
        print(f"\n=== {getattr(item.module, 'TEST', tid)} {time.strftime('%T', time.gmtime())} ===", flush=True)


class TestTimeout(Exception):
    pass


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run the test under a timeout, then turn its recorded gate failures into a pytest failure.
    Every test has a timeout: the module's TIMEOUT(knobs) when it defines one (a soak knows its own duration),
    otherwise --timeout / TEST_TIMEOUT. On expiry the test fails and the fixtures still disconnect the client."""
    funcargs = pyfuncitem.funcargs
    args = {a: funcargs[a] for a in pyfuncitem._fixtureinfo.argnames}
    state = pyfuncitem.config._suite
    limit = state.cfg.test_timeout
    fn = getattr(pyfuncitem.module, "TIMEOUT", None)
    if callable(fn) and "knobs" in funcargs:
        limit = int(fn(funcargs["knobs"]))

    def on_alarm(signum, frame):
        raise TestTimeout(f"test exceeded its timeout of {limit}s")

    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(limit)
    try:
        pyfuncitem.obj(**args)
    except TestTimeout as e:
        checks = funcargs.get("checks")
        if checks is not None and checks.kind == "gate":
            checks.failed(str(e))          # a non-gate gets its one WARN from the makereport hook instead
        pytest.fail(str(e), pytrace=False)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    checks = funcargs.get("checks")
    if checks is not None:
        checks.conclude()
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    state = item.config._suite
    tid = _test_id(item)
    if rep.when != "call" or not tid or state.run is None:
        return
    kind = getattr(item.module, "KIND", "gate")
    if rep.failed and kind != "gate":
        # only a gate may fail the run: a diagnostic or runbook item that raised (timeout, exception) is recorded as
        # WARN and its pytest outcome rewritten, so the process exit code stays a gate-only signal
        full = str(rep.longrepr).strip() if rep.longrepr else "raised"
        msg = full.splitlines()[-1][:300]
        state.run.verdict(getattr(item.module, "TEST", tid), "WARN", kind, f"{kind} raised instead of recording: {msg}")
        print(f"WARN {getattr(item.module, 'TEST', tid)}: {kind} raised instead of recording: {msg}\n{full}", flush=True)   # the traceback stays in console.log
        rep.outcome = "passed"
        rep.longrepr = None
    rc = 1 if rep.failed else 0
    with open(state.run / "run.txt", "a") as r:
        r.write(f"{time.strftime('%T', time.gmtime())} {tid} rc={rc} took={int(rep.duration)}s\n")
    if tid == "t01" and rep.failed:
        state.aborted = True
        print("preconditions failed - aborting the run", flush=True)


def pytest_sessionfinish(session, exitstatus):
    state = getattr(session.config, "_suite", None)
    if state is None or state.run is None:
        return
    clientlib._disarm_all()
    unused = state.cfg.unused_cli_knobs()
    if unused:
        print(f"WARNING: --knob {' '.join(unused)}: no test read these names (typo?)", flush=True)
    if session.config.option.xmlpath:
        print(f"junit: {session.config.option.xmlpath}")
    gate_fail = write_summary(state.run)
    with open(state.run / "run.txt", "a") as r:
        r.write(f"finished={time.strftime('%FT%TZ', time.gmtime())} fail={int(bool(gate_fail))}\n")


# ---------------------------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def cfg(request):
    return request.config._suite.cfg


@pytest.fixture(scope="session")
def run(request, tmp_path_factory):
    state = request.config._suite
    if state.run is None:     # offline self-tests
        state.run = RunDir(tmp_path_factory.mktemp("run"), state.cfg.cell)
    return state.run


@pytest.fixture(scope="session")
def client(cfg, run):
    """The primary client container."""
    return clientlib.Client(cfg, run, index=1)


@pytest.fixture(scope="session")
def clients(cfg, run, client):
    """Every consecutive client container that is running, primary first (CLIENT_COUNT + just clients-start)."""
    others = clientlib.clients_running(cfg, run)
    return [client] + others[1:] if others else [client]


@pytest.fixture(scope="session")
def client2(cfg, run):
    """The second client (T19-background-load, T21-passive-observer), or None when it is not running."""
    c = clientlib.Client(cfg, run, name=cfg.client2, index=2)
    return c if c.exists() else None


@pytest.fixture(scope="session")
def cluster(cfg):
    """The localcluster. On a production-network run (--no-cluster) it reports available() False: tests whose
    subject is the cluster skip, and the node sampler samples nothing."""
    return Cluster(cfg)


@pytest.fixture
def live_cluster(cluster, checks):
    """A cluster that must be there; skips the test otherwise."""
    if not cluster.available():
        checks.skip("needs the localcluster (this is a --no-cluster / production-network run)")
    return cluster


@pytest.fixture(scope="session")
def target(cfg):
    """The in-cluster traffic target; skips the test when it is not running (just target-start)."""
    t = Target(cfg)
    if not t.running():
        pytest.skip(f"target container {cfg.target_name} not running (just target-start)")
    return t


@pytest.fixture(scope="session")
def stack_key(cfg, run, client):
    """Stable id of the software+config under test (provenance in messages and rows)."""
    import hashlib
    from suitelib import shell
    if cfg.env.get("SUITE_STACK_KEY"):
        return cfg.env["SUITE_STACK_KEY"]
    c = client.version()
    h = (shell.out([cfg.hoprd_bin, "--version"], timeout=30) or "unknown-hoprd").splitlines()[0]
    s = (shell.out(["docker", "exec", cfg.server, "./gnosis_vpn-server", "--version"], timeout=30) or "unknown-server").splitlines()[0]
    key = hashlib.sha256("|".join([c, h, s, cfg.cluster_env, cfg.client_extra_env, str(cfg.cluster_size)]).encode()).hexdigest()[:12]
    with open(run / "run.txt", "a") as r:
        r.write(f"stack_key={key}\n")
    return key


@pytest.fixture
def checks(request, run):
    """The verdict recorder of the current test (TEST and KIND come from the test module)."""
    m = request.module
    return Checks(run, getattr(m, "TEST", request.node.name), getattr(m, "KIND", "gate"))


@pytest.fixture
def knobs(request, cfg):
    """The test module's KNOBS resolved for this run (environment, --knob, --fast, --very-fast)."""
    m = request.module
    prefix = getattr(m, "TEST", "").split("-")[0]
    k = cfg.knobs(prefix, getattr(m, "KNOBS", {}))
    if k:
        log(f"knobs: {k.describe()}")
    return k


@pytest.fixture(autouse=True)
def _leave_no_session_behind():
    """Whatever a test did, no client stays connected (and no deadman armed) when it is over."""
    yield
    for c in list(clientlib._all_clients):
        if c.session is not None:
            log(f"{c.name}: still connected at test end - disconnecting")
            c.disconnect()
        c.disarm_deadman()
        c.kill_probes()      # a timeout kills the docker exec, not the probe inside the container
