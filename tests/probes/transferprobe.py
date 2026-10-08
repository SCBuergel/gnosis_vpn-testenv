#!/usr/bin/env python3
"""One HTTP transfer against the traffic target (docker/target/speedtarget.py) with a byte log, for concurrency
ladders that must know which bytes moved while every client was transferring (suitelib/relaybench.py).

    transferprobe.py --host 198.18.0.2 --dir down --bytes 100000000 --start-at 1727700000.0 --timeout 300 --iface wg0

The probe sleeps until --start-at (epoch seconds, the same for every client of a rung, so all transfers start
together however long the runner took to reach each client), then downloads GET /down?bytes=N or uploads N bytes to
POST /up in 64 KiB chunks, and writes one JSON object to stdout:

    {"dir", "want", "start": request sent, "first": first byte moved, "last": last byte moved, "done": response read,
     "bytes", "acked", "counted", "code", "complete", "error", "tunnel": {...}, "samples": [[epoch, cumulative bytes], ...],
     "sent_samples": upload only, the same log for the bytes handed to the socket}

Every time is epoch seconds (time.time()), so logs from several machines line up to their clock sync (NTP). A sample
is taken every --tick seconds and at the first and last byte.

What a sample counts. Download: bytes read from the socket. Upload: bytes the target's TCP has ACKNOWLEDGED
("counted": "acked"): the bytes handed to the socket minus the kernel's send queue (SIOCOUTQ, unsent plus
unacknowledged). The first version counted bytes as handed to the socket: the send buffer (autotuned to several MB)
fills in the first second, so the log ran ahead of the wire, the upload's `last` was the last send() and not the last
byte delivered, and every upload rate was too high by the buffer's share. `last` is now the moment the last byte was
acknowledged, `done` the moment the target's 200 was read; `sent_samples` keeps the old count next to the new one, so
a run can say how far apart the two are. Where the kernel does not answer SIOCOUTQ the probe falls
back to counting handed bytes, says "counted": "sent", and takes `last` from the response.

--iface binds the connection to the tunnel interface (SO_BINDTODEVICE, as the UDP probes do), so a transfer can never
leave by another route, and the interface's own rx/tx byte counters around the transfer are reported in `tunnel`:
a complete transfer must have moved at least its bytes through that interface.

`complete` needs every byte and a 200 (for an upload, the target's own count must match too). The transfer stops at
--timeout seconds after its start, complete or not.

--ping HOST runs `ping -D -O -i PING_INTERVAL` (bound to --iface when given) from the transfer's start to its end, so
the round-trip time under load comes with the byte log: `pings` is [[icmp_seq, epoch sent, rtt ms or null], ...], one
entry per echo sent (null: no reply before the transfer ended). An answered echo's send time is its reply's stamp
minus its rtt; an unanswered one's is extrapolated from the ping's start at the interval."""
import argparse
import fcntl
import http.client
import json
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import time

CHUNK = 65536
PING_REPLY = re.compile(r"^\[(\d+\.\d+)\].*icmp_seq=(\d+).*time=([\d.]+) ms")
PING_SENT = re.compile(r"(\d+) packets transmitted")


def ping_start(a):
    if not a.ping:
        return None
    argv = ["ping", "-n", "-D", "-O", "-i", str(a.ping_interval)] + (["-I", a.iface] if a.iface else []) + [a.ping]
    try:
        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True), time.time()
    except OSError:
        return None


def ping_stop(p, interval):
    """[[seq, epoch sent, rtt ms or None], ...] for every echo the ping sent."""
    if p is None:
        return None
    proc, t0 = p
    proc.send_signal(signal.SIGINT)
    try:
        text = proc.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        text = proc.communicate()[0]
    got = {}
    for line in text.splitlines():
        m = PING_REPLY.match(line)
        if m:
            got[int(m.group(2))] = (float(m.group(1)) - float(m.group(3)) / 1000, float(m.group(3)))
    m = PING_SENT.search(text)
    sent = max([int(m.group(1)) if m else 0] + list(got))
    return [[seq, round(got[seq][0] if seq in got else t0 + (seq - 1) * interval, 3), got[seq][1] if seq in got else None]
            for seq in range(1, sent + 1)]


def iface_bytes(iface):
    """(ifindex, rx_bytes, tx_bytes) of an interface, or None when it is not there."""
    try:
        base = f"/sys/class/net/{iface}/"
        with open(base + "ifindex") as a, open(base + "statistics/rx_bytes") as b, open(base + "statistics/tx_bytes") as c:
            return int(a.read()), int(b.read()), int(c.read())
    except (OSError, ValueError):
        return None


def send_queue(sk):
    """Bytes in the socket's send queue, unsent plus sent-but-unacknowledged (SIOCOUTQ); None when unsupported."""
    try:
        return struct.unpack("i", fcntl.ioctl(sk.fileno(), termios.TIOCOUTQ, b"\0" * 4))[0]
    except OSError:
        return None


def connect(a, timeout):
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if a.iface:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, a.iface.encode())
    sk.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sk.settimeout(timeout)
    sk.connect((a.host, a.port))
    conn = http.client.HTTPConnection(a.host, a.port, timeout=timeout)
    conn.sock = sk
    return conn, sk


def run(a):
    out = {"dir": a.dir, "want": a.bytes, "start": None, "first": None, "last": None, "done": None, "bytes": 0, "acked": None,
           "counted": "read" if a.dir == "down" else "acked", "code": None, "complete": False, "error": None, "tunnel": None,
           "samples": []}
    delay = a.start_at - time.time()
    if delay > 0:
        time.sleep(delay)
    pinger = ping_start(a)
    t0 = time.time()
    out["start"] = t0
    deadline = t0 + a.timeout
    before = iface_bytes(a.iface) if a.iface else None
    moved, next_tick = 0, t0        # moved: the bytes a sample counts (read on a download, acknowledged on an upload)

    def note(now, force=False):
        nonlocal next_tick
        if force or now >= next_tick:
            out["samples"].append([round(now, 3), moved])
            if a.dir == "up":
                out["sent_samples"].append([round(now, 3), total])
            next_tick = now + a.tick

    def progress(now, value):
        """Book `value` cumulative bytes at `now`; first and last follow the bytes, not the calls."""
        nonlocal moved
        if value > moved:
            if out["first"] is None:
                out["first"] = now
                note(now, force=True)       # the zero point of the log: the moment the first byte moved
            moved = value
            out["last"] = now
        note(now)

    conn = sk = None
    total = 0
    if a.dir == "up":
        out["sent_samples"] = []
    try:
        conn, sk = connect(a, max(5.0, min(30.0, a.timeout)))
        if a.dir == "down":
            conn.request("GET", f"/down?bytes={a.bytes}")
            r = conn.getresponse()
            out["code"] = r.status
            while total < a.bytes and time.time() < deadline:
                d = r.read(min(CHUNK, a.bytes - total))
                if not d:
                    break
                total += len(d)
                progress(time.time(), total)
            out["done"] = time.time()
        else:
            block = bytes(CHUNK)
            conn.putrequest("POST", "/up")
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(a.bytes))
            conn.endheaders()
            if send_queue(sk) is None:
                out["counted"] = "sent"

            def acked():
                q = send_queue(sk)
                return total if q is None else max(0, min(total, total - q))

            while total < a.bytes and time.time() < deadline:
                n = min(CHUNK, a.bytes - total)
                conn.send(block[:n])
                total += n
                progress(time.time(), acked())
            # everything is handed to the kernel; the log follows the acknowledgements until the target answers
            while total == a.bytes and time.time() < deadline:
                ready = select.select([sk], [], [], a.tick)[0]
                progress(time.time(), acked())
                if ready:
                    break
            if total == a.bytes and time.time() < deadline:
                r = conn.getresponse()
                out["code"] = r.status
                try:
                    got = json.loads(r.read() or b"{}").get("received")
                except ValueError:
                    got = None
                now = time.time()
                out["done"] = now
                if got is not None and got != total:
                    out["error"] = f"target received {got} of {total}"
                elif r.status == 200:
                    progress(now, total)    # the target has read every byte: whatever SIOCOUTQ still held is delivered
            out["acked"] = moved
    except (OSError, http.client.HTTPException) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            conn.close()
        elif sk is not None:
            sk.close()
    out["bytes"] = total
    if a.ping:
        out["pings"] = ping_stop(pinger, a.ping_interval)
    if out["last"] is not None:
        note(out["last"], force=True)
    if a.iface:
        after = iface_bytes(a.iface)
        ok = before is not None and after is not None and before[0] == after[0]
        out["tunnel"] = {"iface": a.iface, "rx": after[1] - before[1] if ok else None, "tx": after[2] - before[2] if ok else None,
                         "recreated": bool(before and after and before[0] != after[0]), "present": after is not None}
    out["complete"] = total >= a.bytes and moved >= a.bytes and out["code"] == 200 and out["error"] is None
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--dir", choices=("down", "up"), required=True)
    ap.add_argument("--bytes", type=int, required=True)
    ap.add_argument("--start-at", type=float, default=0.0, help="epoch seconds to start at (0: at once)")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--tick", type=float, default=0.25)
    ap.add_argument("--iface", default="", help="the tunnel interface: bind to it and report its byte counters")
    ap.add_argument("--ping", default="", help="ping this host for the length of the transfer (see above)")
    ap.add_argument("--ping-interval", type=float, default=1.0)
    json.dump(run(ap.parse_args()), sys.stdout)


if __name__ == "__main__":
    main()
