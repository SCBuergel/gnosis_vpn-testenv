"""Line-based TOML section editing for client-config cells (T11-capability-matrix, T12-balancer-sweep).
Sections are matched on the exact header line. No table-array support."""
import re


def _blocks(lines):
    """(start, end) of every section block, end exclusive."""
    idx = [i for i, l in enumerate(lines) if re.match(r"^\s*\[", l)] + [len(lines)]
    return list(zip(idx, idx[1:]))


def set_section(path, header, *body):
    """Replace the section with this header (or append it) by header + body lines."""
    with open(path) as fh:
        lines = fh.read().split("\n")
    new = [header, *body, ""]
    for a, b in _blocks(lines):
        if lines[a].strip() == header:
            lines[a:b] = new
            break
    else:
        lines += ["", *new]
    with open(path, "w") as fh:
        fh.write("\n".join(lines))


def del_section(path, header):
    with open(path) as fh:
        lines = fh.read().split("\n")
    for a, b in _blocks(lines):
        if lines[a].strip() == header:
            del lines[a:b]
            break
    with open(path, "w") as fh:
        fh.write("\n".join(lines))


def keep_destinations(path, n):
    """Keep only the first n [destinations.*] blocks."""
    with open(path) as fh:
        lines = fh.read().split("\n")
    seen = 0
    cut = set()
    for a, b in _blocks(lines):
        if lines[a].strip().startswith("[destinations."):
            seen += 1
            if seen > n:
                cut.update(range(a, b))
    with open(path, "w") as fh:
        fh.write("\n".join(l for i, l in enumerate(lines) if i not in cut))


def section_value(path, header, key):
    """The value of key inside the section, or None."""
    inside = False
    with open(path) as fh:
        lines = fh.read().split("\n")
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            inside = s == header
            continue
        if inside:
            m = re.match(rf"^{re.escape(key)}\s*=\s*(.*)$", s)
            if m:
                return m.group(1).strip().strip('"')
    return None
