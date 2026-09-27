"""Line-based TOML section editing for client-config cells (T11-capability-matrix, T12-balancer-sweep).
Sections are matched on the exact header line. No table-array support."""
import re
import tomllib


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
    """The value of key inside the section ("[a.b]"), as a string, or None when the section or key is missing.
    Read with tomllib, so an inline comment, a single-quoted string or an escape is handled by the parser (the
    line-based read used to strip only double quotes and returned `127.0.0.1:51821" # comment` for a commented
    line); the editing functions above stay line-based on purpose, to keep the file's layout."""
    with open(path, "rb") as fh:
        node = tomllib.load(fh)
    for part in header.strip().strip("[]").split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if not isinstance(node, dict) or key not in node:
        return None
    return str(node[key])
