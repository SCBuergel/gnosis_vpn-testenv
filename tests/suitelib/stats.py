"""Small numeric helpers shared by the tests."""
import statistics as st


def floats(values):
    out = []
    for x in values:
        if x in ("", "NA", "None", None):
            continue
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out


def stats(values):
    """{"n","min","median","mean","max","stdev"} (population stdev), or {"n":0}."""
    v = floats(values)
    if not v:
        return {"n": 0}
    return {"n": len(v), "min": min(v), "median": st.median(v), "mean": round(st.mean(v), 3),
            "max": max(v), "stdev": round(st.pstdev(v), 3)}


def pct(values, p):
    """The p-quantile (0..1) of values by rank, or None when empty."""
    v = sorted(floats(values))
    return v[min(len(v) - 1, int(p * len(v)))] if v else None


def p95(values):
    return pct(values, 0.95) or 0


def num(x, default=0.0):
    """float(x) with a default for '', None and non-numbers."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default
