"""Shared by the three UDP target services (callecho, streamsrv, callsrv)."""
import socket

BUF = 8 << 20


def bind_udp(port, host="0.0.0.0"):
    """A UDP server socket. The 8 MB buffers are deliberate: the mixnet delivers in bursts after a stall, and with
    the kernel default (~200 kB) a burst of a few hundred datagrams overflows the queue; a kernel drop on the
    target would then be booked as tunnel loss by the probe. REUSEADDR lets a restarted service rebind at once."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUF)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, BUF)
    s.bind((host, port))
    return s


def pct_ms(values, p):
    v = sorted(values)
    return round(v[min(len(v) - 1, int(p * len(v)))] * 1000, 1) if v else None


def quantiles_ms(values):
    return {"p50": pct_ms(values, .5), "p90": pct_ms(values, .9), "p99": pct_ms(values, .99), "max": pct_ms(values, 1.0)}


def stall_stats(gaps, worst=3):
    """gaps: [(t_last, seconds)]; worst is capped so a UDP reply stays well under the tunnel MTU."""
    return {"stalls_gt_1s": len(gaps), "stalls_gt_2s": sum(1 for g in gaps if g[1] > 2), "stalls_gt_5s": sum(1 for g in gaps if g[1] > 5),
            "stall_total_s": round(sum(g[1] for g in gaps), 1), "worst_stalls": sorted(gaps, key=lambda g: -g[1])[:worst]}
