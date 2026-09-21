#!/usr/bin/env python3
"""Run the suite across version/config cells (T25-knob-ab / T26-version-matrix).

  matrix.py CELLS_FILE [--reverse] [run.py args...]

CELLS_FILE: one cell per line, "name|CLIENT_IMAGE|HOPRD_BIN|CLUSTER_ENV|CLIENT_EXTRA_ENV|LOCALCLUSTER_BIN" (empty
fields allowed, '#' comments). For every cell: just down, bring the stack up with the cell's settings
(up-nobuild), wait for the destination to be Ready, run the suite with SUITE_CELL=name, then just down.
--reverse runs the cells in reverse order (second pass). Results: SUITE_OUT_DIR/<stamp>-<cell>/ per cell and
SUITE_OUT_DIR/matrix-<stamp>.csv across cells."""
import csv
import glob
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTENV = HERE.parent.parent


def _run(cmd, timeout, **kw):
    """subprocess.run with a timeout that never raises: a hung step is a failed step (returncode 124), and the whole
    process group is killed so a hung run.py does not leave its pytest and probes behind."""
    p = subprocess.Popen(cmd, shell=isinstance(cmd, str), start_new_session=True,
                         stdout=subprocess.PIPE if kw.pop("capture_output", False) else None,
                         stderr=subprocess.STDOUT if "stdout" not in kw else None, text=kw.pop("text", True), env=kw.get("env"), cwd=kw.get("cwd"))
    try:
        out, _ = p.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, p.returncode, out or "", "")
    except subprocess.TimeoutExpired:
        print(f"timeout after {timeout}s: {cmd if isinstance(cmd, str) else ' '.join(map(str, cmd))}", flush=True)
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=30)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return subprocess.CompletedProcess(cmd, 124, "", "")


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cells_file, rest = argv[0], argv[1:]
    reverse = "--reverse" in rest
    extra = [a for a in rest if a != "--reverse"]
    out = Path(os.environ.get("SUITE_OUT_DIR", "/tmp/gnosis_vpn-testenv-suite"))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    lines = [l.strip() for l in open(cells_file) if l.strip() and not l.strip().startswith("#")]
    if reverse:
        lines.reverse()
    fail = 0
    for line in lines:
        f = (line.split("|") + [""] * 6)[:6]
        name, cimg, hbin, cenv, clenv, lcbin = f
        lcbin = lcbin or os.environ.get("LOCALCLUSTER_BIN", "")
        env = {**os.environ, "CLIENT_IMAGE": cimg, "HOPRD_BIN": hbin, "LOCALCLUSTER_BIN": lcbin, "CLUSTER_ENV": cenv,
               "CLIENT_EXTRA_ENV": clenv, "SUITE_CELL": name}
        print(f"##### cell {name}: client={cimg} hoprd={hbin} cluster_env='{cenv}' client_env='{clenv}' localcluster={lcbin} "
              f"({time.strftime('%T', time.gmtime())})", flush=True)
        _run("just down", cwd=TESTENV, capture_output=True, timeout=900)
        if _run("just up-nobuild", cwd=TESTENV, env=env, timeout=1800).returncode != 0:
            print(f"cell {name}: stack failed to come up", flush=True)
            with open(out / f"matrix-{stamp}.log", "a") as lg:
                lg.write(f"cell {name}: stack failed to come up\n")
            fail = 1
            continue
        if int(os.environ.get("EXTRA_IDENTITIES", "1")) >= 2:
            _run("just client2-start", cwd=TESTENV, env=env, timeout=600)
        # a fresh client syncs and health-checks before any destination is Ready; give it up to READY_TIMEOUT s
        dest = os.environ.get("DEST", "node-0")
        ready_timeout = int(os.environ.get("READY_TIMEOUT", "600"))
        waited = 0
        while waited < ready_timeout:
            st = _run(["docker", "exec", "gnosis_vpn-client", "gnosis_vpn-ctl", "status"], capture_output=True, text=True, timeout=60).stdout
            if f"{dest} Route health: Ready" in st:
                break
            time.sleep(10)
            waited += 10
        print(f"cell {name}: {dest} Ready after {waited}s", flush=True)
        rc = _run([sys.executable, str(HERE / "run.py"), "--cell", name, "--run-id", f"{stamp}-{name}", *extra],
                  cwd=TESTENV, env=env, timeout=int(os.environ.get("MATRIX_CELL_TIMEOUT", "43200"))).returncode
        fail = fail or (1 if rc else 0)
        _run("just down", cwd=TESTENV, env=env, capture_output=True, timeout=900)
    rows = []
    for d in sorted(glob.glob(f"{out}/{stamp}-*")):
        try:
            for l in open(f"{d}/verdicts.jsonl"):
                if l.strip():
                    v = json.loads(l)
                    rows.append([v.get("cell"), v["test"], v["status"], v["msg"]])
        except FileNotFoundError:
            pass
    with open(out / f"matrix-{stamp}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cell", "test", "status", "msg"])
        w.writerows(rows)
    print(f"matrix summary: {out}/matrix-{stamp}.csv ({len(rows)} verdicts)")
    return fail


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
