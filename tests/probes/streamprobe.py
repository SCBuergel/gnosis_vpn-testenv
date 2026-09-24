#!/usr/bin/env python3
"""One-directional paced UDP stream through the tunnel. --mode ul: send to the stream server, which measures.
--mode dl: ask the server to stream to us and measure here. Stall = gap > 1 s in the received stream while the
sender continued. Delay is one-way delay above the minimum observed (clock offset cancels). The local port is kept
across rebinds so the server keeps streaming to the same address, and the dl keepalive lets the server re-learn
the address if it did change. See probelib for the tunnel-interface binding and the rebind accounting."""
import argparse
import json
import os
import random
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probelib  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--mode", choices=["ul", "dl"], required=True)
ap.add_argument("--host", required=True)
ap.add_argument("--port", type=int, default=8902)
ap.add_argument("--rate-mbit", type=float, required=True)
ap.add_argument("--duration", type=float, required=True)
ap.add_argument("--size", type=int, default=1200)
ap.add_argument("--iface", default="wg0_gnosisvpn", help="tunnel interface to bind to ('' = unbound)")
ap.add_argument("--out", required=True)
ap.add_argument("--local-port", type=int, default=0, help="fixed local port (0 = pick once, then keep it across rebinds)")
ap.add_argument("--dump", default=None, help="dl mode: write seq,delay_ms per packet")
a = ap.parse_args()
dst = (a.host, a.port)
sid = random.getrandbits(32)
pps = a.rate_mbit * 1e6 / 8 / a.size
ts = probelib.TunnelSocket(a.iface, a.local_port, timeout=2.0 if a.mode == "ul" else 1.0)
ts.watch()
start = time.time()
summ = {"mode": a.mode, "rate_mbit": a.rate_mbit, "size": a.size, "pps": round(pps, 1), "sid": sid, "local_port": ts.port, "start": start}

if a.mode == "ul":
    pad = b"x" * (a.size - 20)
    sent_ok = {"n": 0, "failed": 0}

    def send(n):
        try:
            ts.sock.sendto(b"UPLD" + struct.pack("!IId", sid, n, time.time()) + pad, dst)
            sent_ok["n"] += 1
        except OSError:
            sent_ok["failed"] += 1        # interface gone: a send failure, reported next to loss, never inside it
    n = probelib.paced(a.rate_mbit, a.size, a.duration, send)
    n = sent_ok["n"]                       # `sent` is what the kernel accepted; the server's loss is over that
    send_end = time.time()
    time.sleep(2.0)
    rep = None
    for _ in range(15):
        sk = ts.sock
        try:
            sk.sendto(b"UPRQ" + struct.pack("!IId", sid, n, send_end), dst)
            d, _ = sk.recvfrom(65535)
            if d[:4] == b"UPRP":
                rep = json.loads(d[4:])
                break
        except OSError:
            continue
    summ.update({"sent": n, "send_failed": sent_ok["failed"], "duration_s": round(send_end - start, 1), "server_report": rep})
    if rep:
        summ.update({k: rep[k] for k in rep if k != "sent"})
else:
    recv, last, gaps, delays, maxseq, sent_total, send_end, lastkeep = 0, None, [], [], -1, None, None, 0
    ctl_sent, next_ctl = 0, 0.0
    deadline = time.monotonic() + a.duration + 15
    while time.monotonic() < deadline:
        # ask the server to stream, re-asking every 0.3 s until the first packet arrives (up to 3 times): the server
        # starts on the first request, so waiting between requests would only queue its first packets and read
        # them late, which inflated the delay tail of every short arm
        if recv == 0 and ctl_sent < 3 and time.monotonic() >= next_ctl:
            try:
                ts.sock.sendto(b"CTLD" + struct.pack("!IfII", sid, pps, a.size, int(a.duration)), dst)
            except OSError:
                pass
            ctl_sent += 1
            next_ctl = time.monotonic() + 0.3
        if time.time() - lastkeep > 3:
            try:
                ts.sock.sendto(b"KEEP" + struct.pack("!I", sid), dst)
            except OSError:
                pass
            lastkeep = time.time()
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
        if tag == b"DLDA" and len(d) >= 20:
            psid, seq, sent_at = struct.unpack("!IId", d[4:20])
            if psid != sid:
                continue
            recv += 1
            delays.append(now - sent_at)
            maxseq = max(maxseq, seq)
            if last is not None and now - last > 1.0:
                gaps.append((round(last, 3), round(now - last, 3)))
            last = now
            ts.note_recv(now)
        elif tag == b"DLND" and len(d) >= 20:
            psid, sent_total, send_end = struct.unpack("!IId", d[4:20])
            break
    if sent_total is None:
        sent_total, send_end = maxseq + 1, start + a.duration + 1.0
        summ["end_marker"] = "lost"
    if last is not None and send_end - last > 1.0:
        gaps.append((round(last, 3), round(send_end - last, 3)))
    dn = probelib.over_min(delays)
    if a.dump:
        open(a.dump, "w").write("".join("%d,%.1f\n" % (i, x * 1000) for i, x in enumerate(dn)))
    summ.update({"sent": sent_total, "recv": recv, "duration_s": round(time.time() - start, 1),
                 "loss_pct": round(100 * (1 - recv / max(1, sent_total)), 2), "delay_over_min_ms": probelib.quantiles_ms(dn),
                 "delayed_pkts_gt_1s": sum(1 for x in dn if x > 1.0)})
    summ.update(probelib.stall_stats(gaps))
ts.finish()
rb = ts.summary()
# In ul mode nothing arrives until the end-of-stream report, so the client cannot time an outage; the server's gap list
# (stalls_gt_*s / worst_stalls in its report) is the outage view for that direction. Report None rather than a number
# that would only measure "time from the interface going away to the report".
if a.mode == "ul":
    rb["outages"], rb["outage_total_s"] = [], None
summ.update(rb)
summ["end"] = time.time()
json.dump(summ, open(a.out, "w"), indent=1)
print(json.dumps(summ))
