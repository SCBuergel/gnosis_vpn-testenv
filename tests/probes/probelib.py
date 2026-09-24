"""Shared code of the UDP probes that run inside the client container (relprobe, streamprobe, callprobe).

TunnelSocket: a UDP socket bound to the tunnel interface with SO_BINDTODEVICE, so it fails closed off-tunnel, and
RE-BOUND whenever the interface is recreated (the client's watchdog reconnect tears the WireGuard interface down and
creates a new one a few seconds later). A socket bound to the removed interface goes blind without any error, and
blind is indistinguishable from loss: every T06-realtime-udp arm once read "loss = (arm length - 50 s) / arm length"
for exactly this reason. Each rebind is counted and the outage it caused (last packet before, first packet after)
is reported next to, not inside, the loss figure. The local port is kept across rebinds so the far end's view of
us does not change. An empty iface means an unbound socket (loopback self-tests).

The 8 MB socket buffers are the same choice as on the target side, for the reason given in docker/target/udpserver.py."""
import socket
import threading
import time

BUF = 8 << 20


def ifindex(iface):
    if not iface:
        return 0
    try:
        return socket.if_nametoindex(iface)
    except OSError:
        return None


def make_socket(iface, local_port=0, timeout=0.2, reuse=True):
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if reuse:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if iface:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
    sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUF)
    sk.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, BUF)
    sk.bind(("0.0.0.0", local_port))
    sk.settimeout(timeout)
    return sk


class TunnelSocket:
    """The socket plus its rebind accounting. `sock` is the current socket; call watch() to start the watcher."""

    def __init__(self, iface, local_port=0, timeout=0.2, reuse=True, on_event=None):
        self.iface, self.timeout, self.reuse = iface, timeout, reuse
        self.on_event = on_event          # optional callback(name, extra) for per-probe event logs
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.events = []
        self.rebinds = 0
        self.outages = []                 # (from, to|None, seconds)
        self.outage_from = None
        self.last_recv = None
        self.ifindex = ifindex(iface)
        self.sock = make_socket(iface, local_port, timeout, reuse)
        self.port = self.sock.getsockname()[1]
        self.event("START", f"ifindex={self.ifindex} port={self.port}")

    def event(self, name, extra=""):
        self.events.append((round(time.time(), 3), f"{name} {extra}".strip() if extra else name))
        if self.on_event:
            self.on_event(name, extra)

    def note_recv(self, now=None):
        """Book a received packet: closes an open outage."""
        now = now or time.time()
        with self.lock:
            if self.outage_from is not None:
                self.outages.append((round(self.outage_from, 3), round(now, 3), round(now - self.outage_from, 3)))
                self.outage_from = None
            self.last_recv = now

    def _open_outage(self):
        with self.lock:
            if self.outage_from is None:
                self.outage_from = self.last_recv or time.time()

    def watch(self, on_rebind=None):
        """Start the watcher thread: rebuild the socket when the interface is recreated, note when it went away."""
        if not self.iface:
            return None
        t = threading.Thread(target=self._watcher, args=(on_rebind,), daemon=True)
        t.start()
        return t

    def _watcher(self, on_rebind):
        while not self.stop.is_set():
            time.sleep(0.5)
            idx = ifindex(self.iface)
            if idx == self.ifindex:
                continue
            if idx is None:
                if self.ifindex is not None:
                    self.event("IFDOWN")
                    self._open_outage()
                self.ifindex = None
                continue
            self.event("IFUP" if self.ifindex is None else "IFCHANGE", str(idx))
            # The new socket is bound on the same port BEFORE the old one is closed, on purpose: if the bind fails the
            # probe keeps the old socket and retries on the next tick instead of being left with none. The two do not
            # conflict even without SO_REUSEADDR: SO_BINDTODEVICE resolves the name to an ifindex at setsockopt time,
            # the old socket holds the removed interface's index and the new one the recreated interface's, and the
            # kernel only treats sockets on the same device as a bind conflict (T10-forced-reconnect measures exactly
            # this rebind, reuse=False, "rebinds 1" and a 70-80 s recovery on every run).
            try:
                new = make_socket(self.iface, self.port, self.timeout, self.reuse)
            except OSError as e:
                self.event("REBIND_FAILED", str(e))
                continue
            old, self.sock, self.ifindex = self.sock, new, idx
            self.rebinds += 1
            self._open_outage()
            self.event("REBIND", f"ifindex={idx}")
            if on_rebind:
                on_rebind(idx)
            try:
                old.close()
            except OSError:
                pass

    def finish(self, end=None):
        """Stop the watcher and close an outage that never recovered inside the run."""
        self.stop.set()
        end = end or time.time()
        with self.lock:
            if self.outage_from is not None:
                self.outages.append((round(self.outage_from, 3), None, round(end - self.outage_from, 3)))
                self.outage_from = None

    def outage_total_s(self):
        return round(sum(o[2] for o in self.outages), 1)

    def summary(self):
        return {"rebinds": self.rebinds, "outages": self.outages, "outage_total_s": self.outage_total_s(), "events": self.events}


def pct_ms(values, p):
    """The p-quantile (0..1) of values in seconds, as rounded milliseconds; None when empty."""
    v = sorted(values)
    return round(v[min(len(v) - 1, int(p * len(v)))] * 1000, 1) if v else None


def quantiles_ms(values):
    return {"p50": pct_ms(values, .5), "p90": pct_ms(values, .9), "p99": pct_ms(values, .99), "max": pct_ms(values, 1.0)}


def over_min(values):
    """Delays above the minimum observed (one-way delay without clock sync: the clock offset cancels)."""
    m = min(values) if values else 0
    return [x - m for x in values]


def stall_stats(gaps):
    """gaps: [(t_last, seconds)] where the stream paused for more than 1 s while the sender continued."""
    return {"stalls_gt_1s": len(gaps), "stalls_gt_2s": sum(1 for g in gaps if g[1] > 2), "stalls_gt_5s": sum(1 for g in gaps if g[1] > 5),
            "stall_total_s": round(sum(g[1] for g in gaps), 1), "worst_stalls": sorted(gaps, key=lambda g: -g[1])[:10]}


def paced(rate_mbit, size, duration, send, stop=None):
    """Send `send(n)` every size*8/rate seconds for `duration` s (monotonic pacing, no burst catch-up). Returns the count."""
    interval = size * 8 / (rate_mbit * 1e6)
    t0 = time.monotonic()
    n = 0
    while not (stop and stop.is_set()):
        now = time.monotonic()
        if now - t0 >= duration:
            break
        target = t0 + n * interval
        if now < target:
            time.sleep(min(target - now, 0.01))
            continue
        send(n)
        n += 1
    return n
