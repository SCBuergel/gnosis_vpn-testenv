#!/usr/bin/env python3
"""One-directional UDP stream server (port 8902). DL: on a CTLD request, streams paced packets to the requester.
UL: counts UPLD packets per session (gaps > 1 s, one-way delay above the minimum) and answers UPRQ with JSON.

Wire format (big-endian): CTLD sid:u32 pps:f32 size:u32 dur:u32 | KEEP sid:u32 | DLDA sid seq:u32 ts:f64 + pad |
DLND sid sent:u32 ts:f64 | UPLD sid seq ts + pad | UPRQ sid sent send_end:f64 -> UPRP + json.
The requester's address is re-learned from every KEEP, so a client whose tunnel reconnected (and whose source
address may have changed) keeps receiving the stream instead of the server streaming into the void."""
import json
import os
import struct
import threading
import time

from udpserver import bind_udp, quantiles_ms, stall_stats


def serve(port, sock=None):
    s = sock or bind_udp(port)
    print(f"streamsrv udp 0.0.0.0:{port}", flush=True)
    ul, dl_started, dl_addr, lock = {}, set(), {}, threading.Lock()

    def dl_stream(addr, sid, pps, size, dur):
        interval = 1.0 / pps
        pad = b"x" * max(0, size - 20)
        t0 = time.monotonic()
        n = 0
        while time.monotonic() - t0 < dur:
            target = t0 + n * interval
            now = time.monotonic()
            if now < target:
                time.sleep(min(target - now, 0.01))
                continue
            try:
                s.sendto(b"DLDA" + struct.pack("!IId", sid, n, time.time()) + pad, dl_addr.get(sid, addr))
            except OSError:
                pass
            n += 1
        for _ in range(8):
            try:
                s.sendto(b"DLND" + struct.pack("!IId", sid, n, time.time()), dl_addr.get(sid, addr))
            except OSError:
                pass
            time.sleep(0.25)
        print(f"DL sid={sid} to {dl_addr.get(sid, addr)} sent={n}", flush=True)
        with lock:                      # the stream is over: forget it, so a long-lived target does not grow per probe
            dl_started.discard(sid)
            dl_addr.pop(sid, None)

    while True:
        try:
            d, a = s.recvfrom(65535)
        except OSError:
            continue
        tag = d[:4]
        now = time.time()
        if tag == b"UPLD" and len(d) >= 20:
            sid, seq, ts = struct.unpack("!IId", d[4:20])
            with lock:
                st = ul.setdefault(sid, {"recv": 0, "last": None, "first": now, "gaps": [], "delays": [], "maxseq": -1})
                st["recv"] += 1
                st["delays"].append(now - ts)
                st["maxseq"] = max(st["maxseq"], seq)
                if st["last"] is not None and now - st["last"] > 1.0:
                    st["gaps"].append((round(st["last"], 3), round(now - st["last"], 3)))
                st["last"] = now
        elif tag == b"CTLD" and len(d) >= 20:
            sid, pps, size, dur = struct.unpack("!IfII", d[4:20])
            dl_addr[sid] = a
            if sid not in dl_started:
                dl_started.add(sid)
                threading.Thread(target=dl_stream, args=(a, sid, pps, size, dur), daemon=True).start()
                print(f"DL start sid={sid} {a} pps={pps} size={size} dur={dur}", flush=True)
        elif tag == b"KEEP" and len(d) >= 8:
            sid = struct.unpack("!I", d[4:8])[0]
            if sid in dl_started and dl_addr.get(sid) != a:
                print(f"DL sid={sid} requester moved {dl_addr.get(sid)} -> {a}", flush=True)
                dl_addr[sid] = a
        elif tag == b"UPRQ" and len(d) >= 20:
            sid, sent, send_end = struct.unpack("!IId", d[4:20])
            with lock:
                st = ul.get(sid)
            if st is None:
                rep = {"recv": 0, "error": "unknown session"}
            else:
                gaps = list(st["gaps"])
                if st["last"] is not None and send_end - st["last"] > 1.0:
                    gaps.append((round(st["last"], 3), round(send_end - st["last"], 3)))
                m = min(st["delays"]) if st["delays"] else 0
                dn = [x - m for x in st["delays"]]
                rep = {"recv": st["recv"], "sent": sent, "loss_pct": round(100 * (1 - st["recv"] / max(1, sent)), 2),
                       "delay_over_min_ms": quantiles_ms(dn), "delayed_pkts_gt_1s": sum(1 for x in dn if x > 1.0)}
                rep.update(stall_stats(gaps))   # worst_stalls capped: a fragmented UDP reply rarely survives the return path
            try:
                s.sendto(b"UPRP" + json.dumps(rep).encode(), a)
            except OSError:
                pass


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "8902")))
