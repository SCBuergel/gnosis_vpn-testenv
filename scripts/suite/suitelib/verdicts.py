"""The run directory and per-test verdicts.

A run id is a directory and every test appends to it: rows.jsonl (every measurement), verdicts.jsonl (every
check), logs/ (client log slices), samples/ (node/CPU samples), probe JSON and per-second CSVs.

Three kinds of test, scored differently. Only a *gate* can fail the run; a *diagnostic* records and never fails
(a FAIL from one is downgraded to WARN); a *runbook* item is kept but in no run. Every threshold a gate holds a
number against is absolute and named (assert_min / assert_max print the knob next to the value)."""
import json
import sys
import time
from pathlib import Path

import pytest

KINDS = ("gate", "diagnostic", "runbook")


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(*parts):
    """Timestamped progress line on stderr (the console log)."""
    print(time.strftime("%H:%M:%S", time.gmtime()), *parts, file=sys.stderr, flush=True)


class RunDir:
    def __init__(self, path, cell=""):
        self.path = Path(path).resolve()   # resolve the 'latest' symlink: the container sees the real dir
        self.cell = cell
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "logs").mkdir(exist_ok=True)
        (self.path / "samples").mkdir(exist_ok=True)
        self.rows_file = self.path / "rows.jsonl"
        self.verdicts_file = self.path / "verdicts.jsonl"
        self.rows_file.touch()
        self.verdicts_file.touch()

    @property
    def in_client(self):
        """The same directory as the client container sees it (SUITE_OUT_DIR is mounted at /suite-out)."""
        return f"/suite-out/{self.path.name}"

    def __truediv__(self, name):
        return self.path / name

    def has_results(self):
        return self.verdicts_file.exists() and self.verdicts_file.stat().st_size > 0

    def row(self, test, **kv):
        rec = {"t": round(time.time(), 3), "cell": self.cell, "test": test}
        rec.update(kv)
        with open(self.rows_file, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def verdict(self, test, status, kind, msg):
        with open(self.verdicts_file, "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "cell": self.cell, "test": test,
                                "status": status, "kind": kind, "msg": msg}) + "\n")

    def verdicts(self):
        return [json.loads(l) for l in open(self.verdicts_file) if l.strip()]

    def rows(self, test=None):
        out = []
        for l in open(self.rows_file):
            if not l.strip():
                continue
            r = json.loads(l)
            if test is None or r.get("test") == test:
                out.append(r)
        return out

    def read_json(self, name, default=None):
        try:
            return json.load(open(self.path / name))
        except (FileNotFoundError, ValueError):
            return default


class Checks:
    """The verdicts of one test. PASS/FAIL/WARN/SKIP per check, RECORDED for a measurement with no pass/fail,
    XFAIL/XPASS for a known defect. conclude() turns any gate FAIL into a pytest failure."""

    def __init__(self, run, test, kind):
        assert kind in KINDS, kind
        self.run, self.test, self.kind = run, test, kind
        self.failures = []
        self.statuses = []

    def _emit(self, status, msg):
        if status == "FAIL" and self.kind != "gate":
            status = "WARN"          # only gates may fail a run
        print(f"{status} {self.test}: {msg}", flush=True)
        self.run.verdict(self.test, status, self.kind, msg)
        self.statuses.append(status)
        if status == "FAIL":
            self.failures.append(msg)
        return status == "PASS"

    def passed(self, msg):
        return self._emit("PASS", msg)

    def failed(self, msg):
        return self._emit("FAIL", msg)

    def warn(self, msg):
        return self._emit("WARN", msg)

    def verdict(self, ok, msg):
        return self._emit("PASS" if ok else "FAIL", msg)

    def record(self, msg):
        """A measurement with no pass/fail. Never scores, never pads a summary."""
        print(f"RECORDED {self.test}: {msg}", flush=True)
        self.run.verdict(self.test, "RECORDED", self.kind, msg)
        self.statuses.append("RECORDED")

    def skip(self, msg):
        """Record SKIP and stop the test (a stack feature the localcluster cannot provide, a missing container)."""
        print(f"SKIP {self.test}: {msg}", flush=True)
        self.run.verdict(self.test, "SKIP", self.kind, msg)
        self.statuses.append("SKIP")
        pytest.skip(msg)

    def xfail(self, fixed_by, holds, msg):
        """A known defect with no fix in the tested stack. holds=True: the broken behaviour was observed -> XFAIL.
        holds=False: it did not occur -> XPASS, so a silent fix is never swallowed."""
        if holds:
            m = f"{msg} [expected until {fixed_by}]"
            print(f"XFAIL {self.test}: {m}", flush=True)
            self.run.verdict(self.test, "XFAIL", self.kind, m)
            self.statuses.append("XFAIL")
        else:
            m = f"{msg} [expected to fail until {fixed_by} - confirm the fix landed, then make this a gate]"
            print(f"XPASS {self.test}: {m}", flush=True)
            self.run.verdict(self.test, "XPASS", self.kind, m)
            self.statuses.append("XPASS")

    def assert_min(self, what, value, unit, knob, limit):
        """PASS iff value >= limit; the verdict names the knob so a reader sees what the number was held against."""
        v = float(value or 0)
        if v >= float(limit):
            return self.passed(f"{what} {value} {unit} >= {knob}={limit}")
        return self.failed(f"{what} {value} {unit} < {knob}={limit}")

    def assert_max(self, what, value, unit, knob, limit):
        v = float(value or 0)
        if v <= float(limit):
            return self.passed(f"{what} {value} {unit} <= {knob}={limit}")
        return self.failed(f"{what} {value} {unit} > {knob}={limit}")

    def row(self, **kv):
        self.run.row(self.test, **kv)

    def log(self, *parts):
        log(*parts)

    def conclude(self):
        if self.failures:
            pytest.fail(f"{len(self.failures)} failed check(s):\n" + "\n".join(f"- {m}" for m in self.failures),
                        pytrace=False)


def write_summary(run):
    """summary.csv and the per-kind status counts; returns the number of gate failures."""
    import collections
    import csv
    v = run.verdicts()
    with open(run / "summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cell", "kind", "test", "status", "msg"])
        for x in v:
            w.writerow([x.get("cell", ""), x.get("kind", ""), x["test"], x["status"], x["msg"]])
    by = collections.defaultdict(collections.Counter)
    for x in v:
        by[x.get("kind", "gate")][x["status"]] += 1
    for kind in KINDS:
        if by.get(kind):
            print(f"{kind:11s}", dict(by[kind]))
    gate_fail = by.get("gate", {}).get("FAIL", 0)
    print("verdict:", "FAILED" if gate_fail else "passed", f"({gate_fail} gate failures)", "->", run / "summary.csv")
    return gate_fail
