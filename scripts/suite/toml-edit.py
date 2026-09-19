#!/usr/bin/env python3
"""Tiny line-based TOML section editor for client-config cells (T11-capability-matrix, T12-balancer-sweep).
  toml-edit.py FILE set-section '[a.b]' 'key = value' ['key2 = v2' ...]   replace or append a section
  toml-edit.py FILE del-section '[a.b]'
  toml-edit.py FILE keep-destinations N                                   keep only the first N [destinations.*] blocks
Sections are matched on the exact header line. No table-array support."""
import sys, re
path, op = sys.argv[1], sys.argv[2]
lines = open(path).read().split("\n")
def blocks(ls):
    """yield (start, end) of every section block; end exclusive"""
    idx = [i for i, l in enumerate(ls) if re.match(r"^\s*\[", l)] + [len(ls)]
    for a, b in zip(idx, idx[1:]):
        yield a, b
if op == "set-section":
    hdr = sys.argv[3]; body = sys.argv[4:]
    new = [hdr] + body + [""]
    for a, b in blocks(lines):
        if lines[a].strip() == hdr:
            lines[a:b] = new; break
    else:
        lines += [""] + new
elif op == "del-section":
    hdr = sys.argv[3]
    for a, b in blocks(lines):
        if lines[a].strip() == hdr:
            del lines[a:b]; break
elif op == "keep-destinations":
    n = int(sys.argv[3]); seen = 0; out = []; i = 0
    bl = list(blocks(lines)); cut = set()
    for a, b in bl:
        if lines[a].strip().startswith("[destinations."):
            seen += 1
            if seen > n: cut.update(range(a, b))
    lines = [l for i, l in enumerate(lines) if i not in cut]
else:
    sys.exit("unknown op " + op)
open(path, "w").write("\n".join(lines))
