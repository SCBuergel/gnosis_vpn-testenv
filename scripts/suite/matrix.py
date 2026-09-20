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
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTENV = HERE.parent.parent


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
        subprocess.run("just down", shell=True, cwd=TESTENV, capture_output=True)
        if subprocess.run("just up-nobuild", shell=True, cwd=TESTENV, env=env).returncode != 0:
            print(f"cell {name}: stack failed to come up", flush=True)
            with open(out / f"matrix-{stamp}.log", "a") as lg:
                lg.write(f"cell {name}: stack failed to come up\n")
            fail = 1
            continue
        if int(os.environ.get("EXTRA_IDENTITIES", "1")) >= 2:
            subprocess.run("just client2-start", shell=True, cwd=TESTENV, env=env)
        # a fresh client syncs and health-checks before any destination is Ready; give it up to READY_TIMEOUT s
        dest = os.environ.get("DEST", "node-0")
        ready_timeout = int(os.environ.get("READY_TIMEOUT", "600"))
        waited = 0
        while waited < ready_timeout:
            st = subprocess.run(["docker", "exec", "gnosis_vpn-client", "gnosis_vpn-ctl", "status"], capture_output=True, text=True).stdout
            if f"{dest} Route health: Ready" in st:
                break
            time.sleep(10)
            waited += 10
        print(f"cell {name}: {dest} Ready after {waited}s", flush=True)
        rc = subprocess.run([sys.executable, str(HERE / "run.py"), "--cell", name, "--run-id", f"{stamp}-{name}", *extra],
                            cwd=TESTENV, env=env).returncode
        fail = fail or (1 if rc else 0)
        subprocess.run("just down", shell=True, cwd=TESTENV, env=env, capture_output=True)
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
