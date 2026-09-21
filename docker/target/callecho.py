#!/usr/bin/env python3
"""UDP echo (port 8901): every datagram goes back to its sender unchanged.

An echo is not a realistic application, and it is not meant to be one: it is the instrument that gives the probe
(relprobe) a per-packet round-trip time and a loss count with no clock sync between the ends, which is what
T06-realtime-udp's call arm, T11-capability-matrix and the T23-sustained-soak background call need. The realistic
two-way call, with independent streams in each direction and per-leg attribution, is callsrv (port 8903)."""
import os

from udpserver import bind_udp


def serve(port, sock=None):
    s = sock or bind_udp(port)
    print(f"callecho udp 0.0.0.0:{port}", flush=True)
    while True:
        try:
            d, a = s.recvfrom(65535)
            s.sendto(d, a)
        except OSError:
            pass


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "8901")))
