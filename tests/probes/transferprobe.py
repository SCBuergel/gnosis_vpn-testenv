#!/usr/bin/env python3
"""One HTTP transfer against the traffic target (docker/target/speedtarget.py) with a byte log, for concurrency
ladders that must know which bytes moved while every client was transferring (suitelib/relaybench.py).

    transferprobe.py --host 198.18.0.2 --dir down --bytes 100000000 --start-at 1727700000.0 --timeout 300

The probe sleeps until --start-at (epoch seconds, the same for every client of a rung, so all transfers start
together however long the runner took to reach each client), then downloads GET /down?bytes=N or uploads N bytes to
POST /up in 64 KiB chunks, and writes one JSON object to stdout:

    {"dir", "want", "start": request sent, "first": first byte moved, "last": last byte moved, "bytes", "code",
     "complete", "error", "samples": [[epoch, cumulative bytes], ...]}

Every time is epoch seconds (time.time()), so logs from several machines line up to their clock sync (NTP, a few
ms). A sample is taken every --tick seconds and at the first and last byte. Upload bytes are counted as handed to
the socket: the kernel's send buffer (a few MB) makes the log run ahead of the wire by at most that much. `complete`
needs every byte and a 200 (for an upload, the target's own count must match too). The transfer stops at --timeout
seconds after its start, complete or not."""
import argparse
import http.client
import json
import sys
import time

CHUNK = 65536


def run(a):
    out = {"dir": a.dir, "want": a.bytes, "start": None, "first": None, "last": None, "bytes": 0, "code": None,
           "complete": False, "error": None, "samples": []}
    delay = a.start_at - time.time()
    if delay > 0:
        time.sleep(delay)
    t0 = time.time()
    out["start"] = t0
    deadline = t0 + a.timeout
    total, next_tick = 0, t0

    def note(now, force=False):
        nonlocal next_tick
        if force or now >= next_tick:
            out["samples"].append([round(now, 3), total])
            next_tick = now + a.tick

    conn = http.client.HTTPConnection(a.host, a.port, timeout=max(5.0, min(30.0, a.timeout)))
    try:
        if a.dir == "down":
            conn.request("GET", f"/down?bytes={a.bytes}")
            r = conn.getresponse()
            out["code"] = r.status
            while total < a.bytes and time.time() < deadline:
                d = r.read(min(CHUNK, a.bytes - total))
                if not d:
                    break
                now = time.time()
                if out["first"] is None:
                    out["first"] = now
                    note(now, force=True)
                total += len(d)
                out["last"] = now
                note(now)
        else:
            block = bytes(CHUNK)
            conn.putrequest("POST", "/up")
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(a.bytes))
            conn.endheaders()
            while total < a.bytes and time.time() < deadline:
                n = min(CHUNK, a.bytes - total)
                conn.send(block[:n])
                now = time.time()
                if out["first"] is None:
                    out["first"] = now
                    note(now, force=True)
                total += n
                out["last"] = now
                note(now)
            if total == a.bytes:
                r = conn.getresponse()
                out["code"] = r.status
                try:
                    got = json.loads(r.read() or b"{}").get("received")
                except ValueError:
                    got = None
                if got is not None and got != total:
                    out["error"] = f"target received {got} of {total}"
    except (OSError, http.client.HTTPException) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        conn.close()
    out["bytes"] = total
    if out["last"] is not None:
        note(out["last"], force=True)
    out["complete"] = total >= a.bytes and out["code"] == 200 and out["error"] is None
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
    json.dump(run(ap.parse_args()), sys.stdout)


if __name__ == "__main__":
    main()
