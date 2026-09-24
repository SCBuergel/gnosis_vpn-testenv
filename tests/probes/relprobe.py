#!/usr/bin/env python3
"""Paced bidirectional UDP echo probe: sends a constant-rate stream to an echo server (upload) and receives the
echoes (download). Reports loss, RTT tail, and "stalls": gaps > 1 s in the echo stream while sending continued.
The echo gives a per-packet RTT without any clock sync between the ends, which is why T06-realtime-udp's call arm
and T23-sustained-soak's background call use it; the independent two-way call is callprobe. See probelib for the
tunnel-interface binding and the rebind accounting."""
import argparse
import json
import os
import statistics
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probelib  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--host", required=True)
ap.add_argument("--port", type=int, default=8901)
ap.add_argument("--rate-mbit", type=float, required=True)
ap.add_argument("--duration", type=float, required=True)
ap.add_argument("--size", type=int, default=1200)
ap.add_argument("--iface", default="wg0_gnosisvpn", help="tunnel interface to bind to ('' = unbound)")
ap.add_argument("--local-port", type=int, default=0, help="fixed local port (0 = pick once, then keep it across rebinds)")
ap.add_argument("--out", required=True)
ap.add_argument("--grace", type=float, default=5.0, help="seconds to keep receiving after the last send (echoes still in flight)")
a = ap.parse_args()
# the arguments are divisors and packet sizes: refuse what would divide by zero or not fit the 12-byte header
if a.rate_mbit <= 0 or a.duration <= 0:
    ap.error("--rate-mbit and --duration must be positive")
if a.size < 13:
    ap.error("--size must be at least 13 bytes (12-byte header plus payload)")
dst = (a.host, a.port)
pps = a.rate_mbit * 1e6 / 8 / a.size
pad = b"x" * (a.size - 12)
ts = probelib.TunnelSocket(a.iface, a.local_port)
lock = threading.Lock()
st = {"sent": 0, "send_failed": 0, "recv": 0, "dup": 0, "rtts": [], "delayed": 0, "gaps": [], "persec": {}}
seen = set()


def sender():
    def send(n):
        # `sent` counts datagrams the kernel accepted; a sendto that fails (interface gone, kill switch) is a
        # send failure, not a packet the path lost, so it is reported next to loss and never inside it
        try:
            ts.sock.sendto(struct.pack("!Id", n, time.time()) + pad, dst)
        except OSError:
            with lock:
                st["send_failed"] += 1
            return
        with lock:
            st["sent"] += 1
    # the sequence number keeps advancing through a send failure, so the far end sees a gap that is not loss;
    # loss is recv over accepted sends, never derived from sequence numbers
    probelib.paced(a.rate_mbit, a.size, a.duration, send, ts.stop)
    st["send_end"] = time.time()      # the receiver keeps listening --grace seconds for the echoes still in flight


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
        if len(d) < 12:
            continue
        now = time.time()
        seq, sent_at = struct.unpack("!Id", d[:12])
        rtt = now - sent_at
        with lock:
            if seq in seen:
                st["dup"] += 1
                continue
            seen.add(seq)
            st["recv"] += 1
            st["rtts"].append(rtt)
            if rtt > 1.0:
                st["delayed"] += 1
            if ts.last_recv is not None and now - ts.last_recv > 1.0:
                st["gaps"].append((round(ts.last_recv, 3), round(now - ts.last_recv, 3)))
            ps = st["persec"].setdefault(int(now), [0, []])
            ps[0] += 1
            ps[1].append(rtt)
        ts.note_recv(now)


ts.watch()
tS, tR = threading.Thread(target=sender, daemon=True), threading.Thread(target=receiver, daemon=True)
start = time.time()
tS.start()
tR.start()
tS.join()
time.sleep(a.grace)
ts.stop.set()
tR.join(1.0)
end = time.time()
send_end = st.get("send_end", end)
ts.finish(send_end)
with lock:
    if ts.last_recv is not None and send_end - ts.last_recv > 1.0:
        st["gaps"].append((round(ts.last_recv, 3), round(send_end - ts.last_recv, 3)))
    r = st["rtts"]
    summ = {"host": a.host, "iface": a.iface, "local_port": ts.port, "rate_mbit": a.rate_mbit, "size": a.size, "pps": round(pps, 1),
            "duration_s": round(end - start, 1), "sent": st["sent"], "send_failed": st["send_failed"], "recv": st["recv"], "dup": st["dup"],
            "loss_pct": round(100 * (1 - st["recv"] / max(1, st["sent"])), 2),
            "rtt_ms": probelib.quantiles_ms(r), "delayed_pkts_rtt_gt_1s": st["delayed"]}
    summ.update(probelib.stall_stats(st["gaps"]))
    summ.update(ts.summary())
    summ.update({"start": start, "end": end})
    with open(a.out + ".csv", "w") as f:
        f.write("t,recv,rtt_p50_ms,rtt_max_ms\n")
        for sec in range(int(start), int(end) + 1):
            n, rl = st["persec"].get(sec, [0, []])
            f.write("%d,%d,%s,%s\n" % (sec, n, round(statistics.median(rl) * 1000, 1) if rl else "", round(max(rl) * 1000, 1) if rl else ""))
with open(a.out + ".json", "w") as fh:
    json.dump(summ, fh, indent=1)
print(json.dumps(summ))
