#!/usr/bin/env python3
"""Two-way call server for the Gnosis VPN attribution test (UDP, port 8903).

The client sends an upstream call stream (video + audio packets). On a CALS request the server
starts an independent downstream call stream to the client at the requested schedule. Every
upstream arrival and every downstream send is logged per packet so that a lost packet can be
attributed to the forward leg (client -> here) or the return leg (here -> client).

Wire format (all big-endian):
  CALS  sid:u32 dur:f32 vpps:f32 vsize:u32 apps:f32 asize:u32    start downstream for sid
  CALU  sid:u32 seq:u32 kind:u8 ts:f64 + pad                     upstream call packet
  CALD  sid:u32 seq:u32 kind:u8 ts:f64 + pad                     downstream call packet
  CALE  sid:u32                                                   end; server replies CALR + json
  CALA  sid:u32                                                   ack of CALS
kind: 0 = video, 1 = audio. seq counts per (sid, kind).
Log: /root/callsrv/<sid>.csv  lines  U,kind,seq,ts_client,t_srv  and  D,kind,seq,t_srv
"""
import json
import os
import socket
import struct
import threading
import time

PORT = int(os.environ.get("PORT", "8903"))
LOGDIR = "/root/callsrv"
os.makedirs(LOGDIR, exist_ok=True)

HDR = struct.Struct("!IIBd")            # sid, seq, kind, ts
CALS = struct.Struct("!IffIfI")         # sid, dur, vpps, vsize, apps, asize

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
s.bind(("0.0.0.0", PORT))
print("callsrv udp 0.0.0.0:%d" % PORT, flush=True)

lock = threading.Lock()
sessions = {}   # sid -> dict(addr, log, recv[kind], sent[kind], started, last_up)


def session(sid, addr):
    st = sessions.get(sid)
    if st is None:
        st = {"addr": addr, "log": open(os.path.join(LOGDIR, "%d.csv" % sid), "a", buffering=1 << 16),
              "recv": [0, 0], "sent": [0, 0], "started": False, "last_up": time.time(),
              "first_up": time.time(), "flush": time.time()}
        sessions[sid] = st
    st["addr"] = addr
    return st


def downstream(sid, dur, vpps, vsize, apps, asize):
    st = sessions[sid]
    log = st["log"]
    vint, aint = 1.0 / vpps, 1.0 / apps
    vpad, apad = b"v" * max(0, vsize - 4 - HDR.size), b"a" * max(0, asize - 4 - HDR.size)
    t0 = time.monotonic()
    nv = na = 0
    paused = False
    while time.monotonic() - t0 < dur:
        # idle-pause mode: stop streaming while the client has been silent for > 2 s,
        # resume on its next upstream packet (so a reconnecting client is not hit at t=0)
        if st.get("idle_pause"):
            silent = time.time() - st["last_up"] > 2.0
            if silent and not paused:
                paused = True
                with lock:
                    log.write("P,%.6f\n" % time.time())
                    log.flush()
            elif not silent and paused:
                paused = False
                with lock:
                    log.write("Q,%.6f\n" % time.time())
                    log.flush()
            if paused:
                time.sleep(0.05)
                t0 += 0.05          # do not burst to catch up after a pause
                continue
        tv, ta = t0 + nv * vint, t0 + na * aint
        now = time.monotonic()
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
            s.sendto(b"CALD" + HDR.pack(sid, seq, kind, ts) + pad, st["addr"])
        except OSError:
            pass
        with lock:
            st["sent"][kind] += 1
            log.write("D,%d,%d,%.6f\n" % (kind, seq, ts))
    print("DL done sid=%d video=%d audio=%d" % (sid, nv, na), flush=True)


def flusher():
    while True:
        time.sleep(1.0)
        with lock:
            for st in sessions.values():
                st["log"].flush()


threading.Thread(target=flusher, daemon=True).start()

while True:
    try:
        d, a = s.recvfrom(65535)
    except Exception:
        continue
    now = time.time()
    tag = d[:4]
    if tag == b"CALU" and len(d) >= 4 + HDR.size:
        sid, seq, kind, ts = HDR.unpack(d[4:4 + HDR.size])
        with lock:
            st = session(sid, a)
            st["recv"][kind] += 1
            st["last_up"] = now
            st["log"].write("U,%d,%d,%.6f,%.6f\n" % (kind, seq, ts, now))
    elif tag == b"CALS" and len(d) >= 4 + CALS.size:
        sid, dur, vpps, vsize, apps, asize = CALS.unpack(d[4:4 + CALS.size])
        idle_pause = len(d) > 4 + CALS.size and d[4 + CALS.size] == 1
        with lock:
            st = session(sid, a)
            st["idle_pause"] = idle_pause
            st["last_up"] = now
            start = not st["started"]
            st["started"] = True
        if start:
            threading.Thread(target=downstream, args=(sid, dur, vpps, vsize, apps, asize), daemon=True).start()
            print("DL start sid=%d %s dur=%.0f v=%.0fpps/%dB a=%.0fpps/%dB idle_pause=%s" % (sid, a, dur, vpps, vsize, apps, asize, idle_pause), flush=True)
        try:
            s.sendto(b"CALA" + struct.pack("!I", sid), a)
        except OSError:
            pass
    elif tag == b"CALE" and len(d) >= 8:
        sid = struct.unpack("!I", d[4:8])[0]
        with lock:
            st = sessions.get(sid)
            rep = {"sid": sid, "recv_video": st["recv"][0], "recv_audio": st["recv"][1],
                   "sent_video": st["sent"][0], "sent_audio": st["sent"][1]} if st else {"sid": sid, "error": "unknown"}
            if st:
                st["log"].flush()
        try:
            s.sendto(b"CALR" + json.dumps(rep).encode(), a)
        except OSError:
            pass
