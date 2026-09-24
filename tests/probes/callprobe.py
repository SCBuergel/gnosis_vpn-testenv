#!/usr/bin/env python3
"""Two-way call client (T10-forced-reconnect). Sends an upstream call stream (video + audio) to callsrv and
receives the server's independent downstream stream. Logs every packet sent and received with timestamps, so the
far end's log can be joined per (kind, seq) to say on which leg a packet was lost. After a rebind the stream
continues with the same session id and the server is re-announced so it learns the new address.

Log: <out>.csv  lines  S,kind,seq,t          upstream packet sent
                       R,kind,seq,t_srv,t    downstream packet received
                       E,t,event             REBIND / IFDOWN / IFUP / START / END
     <out>.json summary. See probelib for the tunnel-interface binding and the rebind accounting."""
import argparse
import json
import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probelib  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--host", required=True)
ap.add_argument("--port", type=int, default=8903)
ap.add_argument("--sid", type=int, required=True)
ap.add_argument("--duration", type=float, required=True)
ap.add_argument("--video-pps", type=float, default=150)
ap.add_argument("--video-size", type=int, default=1100)
ap.add_argument("--audio-pps", type=float, default=50)
ap.add_argument("--audio-size", type=int, default=120)
ap.add_argument("--iface", default="wg0_gnosisvpn", help="tunnel interface to bind to ('' = unbound)")
ap.add_argument("--out", required=True)
ap.add_argument("--rejoin-delay", type=float, default=0.0, help="after a rebind, stay silent this long before sending again")
ap.add_argument("--idle-pause", action="store_true", help="ask the server to pause its stream while we are silent")
a = ap.parse_args()

HDR = struct.Struct("!IIBd")
CALS = struct.Struct("!IffIfI")
dst = (a.host, a.port)
log = open(a.out + ".csv", "w", buffering=1 << 16)
loglock = threading.Lock()
state = {"sent": [0, 0], "send_failed": [0, 0], "recv": [0, 0], "dup": [0, 0], "delay": [[], []], "last_recv": None, "gaps": [],
         "seen": [set(), set()], "started": False, "hold_until": 0.0}


def ev(name, extra=""):
    with loglock:
        log.write("E,%.6f,%s%s\n" % (time.time(), name, ("," + extra) if extra else ""))
        log.flush()


def on_rebind(idx):
    state["started"] = False       # re-announce so the server learns the new address
    state["hold_until"] = time.monotonic() + a.rejoin_delay


ts = probelib.TunnelSocket(a.iface, reuse=False, on_event=ev)


def sender():
    vint, aint = 1.0 / a.video_pps, 1.0 / a.audio_pps
    vpad, apad = b"v" * max(0, a.video_size - 4 - HDR.size), b"a" * max(0, a.audio_size - 4 - HDR.size)
    t0 = time.monotonic()
    nv = na = 0
    last_start = 0.0
    flag = b"\x01" if a.idle_pause else b"\x00"
    while not ts.stop.is_set() and time.monotonic() - t0 < a.duration:
        now = time.monotonic()
        if now < state["hold_until"]:
            time.sleep(0.05)
            t0 += 0.05             # keep the schedule from bursting after the hold
            continue
        if not state["started"] and now - last_start > 1.0:
            last_start = now
            try:
                ts.sock.sendto(b"CALS" + CALS.pack(a.sid, a.duration, a.video_pps, a.video_size, a.audio_pps, a.audio_size) + flag, dst)
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
        sent_at = time.time()
        try:
            ts.sock.sendto(b"CALU" + HDR.pack(a.sid, seq, kind, sent_at) + pad, dst)
        except OSError:
            state["send_failed"][kind] += 1     # interface gone: not a packet the path lost
            continue
        state["sent"][kind] += 1
        with loglock:
            log.write("S,%d,%d,%.6f\n" % (kind, seq, sent_at))
    state["send_end"] = time.time()


def receiver():
    while not ts.stop.is_set():
        sk = ts.sock
        try:
            d, _ = sk.recvfrom(65535)
        except TimeoutError:
            continue
        except OSError:
            time.sleep(0.05)
            continue
        now = time.time()
        tag = d[:4]
        if tag == b"CALD" and len(d) >= 4 + HDR.size:
            sid, seq, kind, sent_at = HDR.unpack(d[4:4 + HDR.size])
            if sid != a.sid:
                continue
            if seq in state["seen"][kind]:
                state["dup"][kind] += 1
                continue
            state["seen"][kind].add(seq)
            state["recv"][kind] += 1
            state["delay"][kind].append(now - sent_at)
            if state["last_recv"] is not None and now - state["last_recv"] > 1.0:
                state["gaps"].append((round(state["last_recv"], 3), round(now - state["last_recv"], 3)))
            state["last_recv"] = now
            ts.note_recv(now)
            with loglock:
                log.write("R,%d,%d,%.6f,%.6f\n" % (kind, seq, sent_at, now))
        elif tag == b"CALA":
            state["started"] = True
            ev("ACK")


def flusher():
    while not ts.stop.is_set():
        time.sleep(1.0)
        with loglock:
            log.flush()


ts.watch(on_rebind)
threads = [threading.Thread(target=f, daemon=True) for f in (sender, receiver, flusher)]
for t in threads:
    t.start()
threads[0].join()
time.sleep(3.0)
ts.finish()
for t in threads:
    t.join(1.5)
# End: ask the server for its counts
rep = None
try:
    sk = probelib.make_socket(a.iface, 0, 2.0, reuse=False)
    for _ in range(3):
        sk.sendto(b"CALE" + struct.pack("!I", a.sid), dst)
        try:
            d, _ = sk.recvfrom(65535)
            if d[:4] == b"CALR":
                rep = json.loads(d[4:].decode())
                break
        except TimeoutError:
            pass
except OSError:
    pass
ev("END")
log.flush()
summ = {"sid": a.sid, "host": a.host, "iface": a.iface, "duration_s": a.duration,
        "video": {"pps": a.video_pps, "size": a.video_size, "sent": state["sent"][0], "send_failed": state["send_failed"][0], "recv": state["recv"][0], "dup": state["dup"][0]},
        "audio": {"pps": a.audio_pps, "size": a.audio_size, "sent": state["sent"][1], "send_failed": state["send_failed"][1], "recv": state["recv"][1], "dup": state["dup"][1]},
        "server": rep, "rebinds": ts.rebinds,
        "down_delay_over_min_ms": {k: probelib.quantiles_ms(probelib.over_min(state["delay"][i])) for i, k in ((0, "video"), (1, "audio"))},
        "down_gaps_gt_1s": len(state["gaps"]), "down_gap_total_s": round(sum(g[1] for g in state["gaps"]), 1),
        "worst_gaps": sorted(state["gaps"], key=lambda g: -g[1])[:10], "outages": ts.outages, "events": ts.events}
if rep:
    summ["up_loss_pct"] = {"video": round(100 * (1 - rep["recv_video"] / max(1, state["sent"][0])), 2),
                           "audio": round(100 * (1 - rep["recv_audio"] / max(1, state["sent"][1])), 2)}
    summ["down_loss_pct"] = {"video": round(100 * (1 - state["recv"][0] / max(1, rep["sent_video"])), 2),
                             "audio": round(100 * (1 - state["recv"][1] / max(1, rep["sent_audio"])), 2)}
json.dump(summ, open(a.out + ".json", "w"), indent=1)
print(json.dumps(summ))
