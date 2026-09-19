#!/usr/bin/env python3
"""Two-way call client for the Gnosis VPN attribution test.

Sends an upstream call stream (video + audio) to callsrv and receives the server's independent
downstream stream. Logs every packet sent and received with timestamps, so the far end's log can
be joined per (kind, seq) to say on which leg a packet was lost.

Bound to the tunnel interface with SO_BINDTODEVICE. If the interface disappears and comes back
(the client's watchdog reconnecting), the socket is rebuilt and the stream continues with the
same session id; the rebind is logged as an event.

Log: <out>.csv  lines  S,kind,seq,t          upstream packet sent
                       R,kind,seq,t_srv,t    downstream packet received
                       E,t,event             REBIND / IFDOWN / IFUP / START / END
     <out>.json summary
"""
import argparse
import json
import os
import socket
import struct
import threading
import time

ap = argparse.ArgumentParser()
ap.add_argument("--host", required=True)
ap.add_argument("--port", type=int, default=8903)
ap.add_argument("--sid", type=int, required=True)
ap.add_argument("--duration", type=float, required=True)
ap.add_argument("--video-pps", type=float, default=150)
ap.add_argument("--video-size", type=int, default=1100)
ap.add_argument("--audio-pps", type=float, default=50)
ap.add_argument("--audio-size", type=int, default=120)
ap.add_argument("--iface", default="wg0_gnosisvpn")
ap.add_argument("--out", required=True)
ap.add_argument("--rejoin-delay", type=float, default=0.0, help="after a rebind, stay silent this long before sending again")
ap.add_argument("--idle-pause", action="store_true", help="ask the server to pause its stream while we are silent")
a = ap.parse_args()

HDR = struct.Struct("!IIBd")
CALS = struct.Struct("!IffIfI")
dst = (a.host, a.port)
log = open(a.out + ".csv", "w", buffering=1 << 16)
loglock = threading.Lock()
stop = threading.Event()
state = {"sock": None, "ifindex": None, "sent": [0, 0], "recv": [0, 0], "dup": [0, 0],
         "delay": [[], []], "rebinds": 0, "last_recv": None, "gaps": [], "seen": [set(), set()],
         "last_sent": [-1, -1]}


def ev(name, extra=""):
    with loglock:
        log.write("E,%.6f,%s%s\n" % (time.time(), name, ("," + extra) if extra else ""))
        log.flush()


def make_socket():
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, a.iface.encode())
    sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sk.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
    sk.settimeout(0.2)
    return sk


def ifindex():
    try:
        return socket.if_nametoindex(a.iface)
    except OSError:
        return None


state["ifindex"] = ifindex()
state["sock"] = make_socket()


def watcher():
    """Rebuild the socket when the tunnel interface is recreated."""
    while not stop.is_set():
        time.sleep(0.5)
        idx = ifindex()
        if idx == state["ifindex"]:
            continue
        if idx is None:
            ev("IFDOWN")
            state["ifindex"] = None
            continue
        ev("IFUP" if state["ifindex"] is None else "IFCHANGE", str(idx))
        try:
            new = make_socket()
        except OSError as e:
            ev("REBIND_FAILED", str(e))
            continue
        old = state["sock"]
        state["sock"] = new
        state["ifindex"] = idx
        state["rebinds"] += 1
        state["started"] = False       # re-announce so the server learns the new address
        state["hold_until"] = time.monotonic() + a.rejoin_delay
        ev("REBIND", "%s,hold=%.0f" % (idx, a.rejoin_delay))
        try:
            old.close()
        except OSError:
            pass


def sender():
    vint, aint = 1.0 / a.video_pps, 1.0 / a.audio_pps
    vpad, apad = b"v" * max(0, a.video_size - 4 - HDR.size), b"a" * max(0, a.audio_size - 4 - HDR.size)
    t0 = time.monotonic()
    nv = na = 0
    last_start = 0.0
    flag = b"\x01" if a.idle_pause else b"\x00"
    while not stop.is_set() and time.monotonic() - t0 < a.duration:
        now = time.monotonic()
        if now < state.get("hold_until", 0.0):
            time.sleep(0.05)
            # keep the schedule from bursting after the hold
            t0 += 0.05
            continue
        if not state.get("started") and now - last_start > 1.0:
            last_start = now
            try:
                state["sock"].sendto(b"CALS" + CALS.pack(a.sid, a.duration, a.video_pps, a.video_size,
                                                          a.audio_pps, a.audio_size) + flag, dst)
            except OSError:
                pass
        tv, ta = t0 + nv * vint, t0 + na * aint
        nxt = min(tv, ta)
        if now < nxt:
            time.sleep(min(nxt - now, 0.005))
            continue
        if tv <= ta:
            kind, seq, pad = 0, nv, vpad
            nv += 1
        else:
            kind, seq, pad = 1, na, apad
            na += 1
        ts = time.time()
        try:
            state["sock"].sendto(b"CALU" + HDR.pack(a.sid, seq, kind, ts) + pad, dst)
        except OSError:
            pass
        state["sent"][kind] += 1
        state["last_sent"][kind] = seq
        with loglock:
            log.write("S,%d,%d,%.6f\n" % (kind, seq, ts))
    state["send_end"] = time.time()


def receiver():
    while not stop.is_set():
        sk = state["sock"]
        try:
            d, _ = sk.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            time.sleep(0.05)
            continue
        now = time.time()
        tag = d[:4]
        if tag == b"CALD" and len(d) >= 4 + HDR.size:
            sid, seq, kind, ts = HDR.unpack(d[4:4 + HDR.size])
            if sid != a.sid:
                continue
            if seq in state["seen"][kind]:
                state["dup"][kind] += 1
                continue
            state["seen"][kind].add(seq)
            state["recv"][kind] += 1
            state["delay"][kind].append(now - ts)
            if state["last_recv"] is not None and now - state["last_recv"] > 1.0:
                state["gaps"].append((round(state["last_recv"], 3), round(now - state["last_recv"], 3)))
            state["last_recv"] = now
            with loglock:
                log.write("R,%d,%d,%.6f,%.6f\n" % (kind, seq, ts, now))
        elif tag == b"CALA":
            state["started"] = True
            ev("ACK")


def flusher():
    while not stop.is_set():
        time.sleep(1.0)
        with loglock:
            log.flush()


ev("START", "sid=%d iface=%s ifindex=%s" % (a.sid, a.iface, state["ifindex"]))
threads = [threading.Thread(target=f, daemon=True) for f in (watcher, sender, receiver, flusher)]
for t in threads:
    t.start()
threads[1].join()
time.sleep(3.0)
stop.set()
for t in threads:
    if t.ident is not None:
        t.join(1.5)
# End: ask the server for its counts
rep = None
try:
    sk = make_socket()
    sk.settimeout(2.0)
    for _ in range(3):
        sk.sendto(b"CALE" + struct.pack("!I", a.sid), dst)
        try:
            d, _ = sk.recvfrom(65535)
            if d[:4] == b"CALR":
                rep = json.loads(d[4:].decode())
                break
        except socket.timeout:
            pass
except OSError:
    pass
ev("END")
log.flush()


def q(v, p):
    v = sorted(v)
    return round(v[min(len(v) - 1, int(p * len(v)))] * 1000, 1) if v else None


def norm(v):
    m = min(v) if v else 0
    return [x - m for x in v]


summ = {"sid": a.sid, "host": a.host, "iface": a.iface, "duration_s": a.duration,
        "video": {"pps": a.video_pps, "size": a.video_size, "sent": state["sent"][0], "recv": state["recv"][0], "dup": state["dup"][0]},
        "audio": {"pps": a.audio_pps, "size": a.audio_size, "sent": state["sent"][1], "recv": state["recv"][1], "dup": state["dup"][1]},
        "server": rep, "rebinds": state["rebinds"],
        "down_delay_over_min_ms": {k: {"p50": q(norm(state["delay"][i]), .5), "p90": q(norm(state["delay"][i]), .9),
                                       "p99": q(norm(state["delay"][i]), .99), "max": q(norm(state["delay"][i]), 1.0)}
                                   for i, k in ((0, "video"), (1, "audio"))},
        "down_gaps_gt_1s": len(state["gaps"]), "down_gap_total_s": round(sum(g[1] for g in state["gaps"]), 1),
        "worst_gaps": sorted(state["gaps"], key=lambda g: -g[1])[:10]}
if rep:
    summ["up_loss_pct"] = {"video": round(100 * (1 - rep["recv_video"] / max(1, state["sent"][0])), 2),
                           "audio": round(100 * (1 - rep["recv_audio"] / max(1, state["sent"][1])), 2)}
    summ["down_loss_pct"] = {"video": round(100 * (1 - state["recv"][0] / max(1, rep["sent_video"])), 2),
                             "audio": round(100 * (1 - state["recv"][1] / max(1, rep["sent_audio"])), 2)}
json.dump(summ, open(a.out + ".json", "w"), indent=1)
print(json.dumps(summ))
