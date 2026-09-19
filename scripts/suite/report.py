#!/usr/bin/env python3
"""report.py RUN_DIR... — one Markdown table of verdicts per test across runs/cells (later runs of the same
cell+test override earlier ones), plus a per-cell PASS/WARN/FAIL/SKIP count. Reads verdicts.jsonl."""
import sys, json, glob, os, collections
runs = []
for arg in sys.argv[1:]:
    runs += sorted(glob.glob(arg)) if any(c in arg for c in "*?[") else [arg]
best = {}   # (cell,test) -> list of verdict dicts of the latest run that has this test
order = []
for run in runs:
    try:
        v = [json.loads(l) for l in open(os.path.join(run, "verdicts.jsonl")) if l.strip()]
    except FileNotFoundError:
        continue
    by = collections.defaultdict(list)
    for x in v: by[(x.get("cell",""), x["test"])].append(x)
    for k, lst in by.items():
        best[k] = lst
        if k not in order: order.append(k)
cells = sorted({k[0] for k in best}); tests = sorted({k[1] for k in best})
def status(lst):
    s = {x["status"] for x in lst}
    return "FAIL" if "FAIL" in s else "WARN" if "WARN" in s else "PASS" if "PASS" in s else "SKIP"
print("| test | " + " | ".join(cells) + " |"); print("|---|" + "---|" * len(cells))
for t in tests:
    print(f"| {t} | " + " | ".join(status(best[(c,t)]) if (c,t) in best else "–" for c in cells) + " |")
print()
for c in cells:
    cnt = collections.Counter(status(best[(c,t)]) for t in tests if (c,t) in best)
    print(f"**{c}**: " + ", ".join(f"{k} {v}" for k, v in sorted(cnt.items())))
print()
for c in cells:
    print(f"### {c}")
    for t in tests:
        if (c,t) not in best: continue
        for x in best[(c,t)]:
            print(f"- {x['status']} {t}: {x['msg']}")
