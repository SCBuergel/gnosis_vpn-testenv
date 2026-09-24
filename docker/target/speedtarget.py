#!/usr/bin/env python3
"""Sized HTTP download/upload target (port 8899) for the transfer tests.

  GET  /down?bytes=N   -> N bytes of fixed pseudo-random content (default 25000000, capped at 200 MB)
  POST /up             -> reads and discards the body, returns 200 with the byte count
  GET  /health         -> ok

This is deliberately a target we own inside the cluster and not a public speed-test provider, rotated or not:
every VPN client emerging from one exit shares that exit's egress IP, and a public provider rate-limits by
source IP (speed.cloudflare.com returned 429 with Retry-After 3071 s to ALL clients of one exit after ~45 min
of testing, which also rate-limited the exit's real users). A local target has no limit, gives a symmetric upload
endpoint, and keeps every run comparable with every other. The content is pseudo-random (one 1 MiB block
repeated), so a deflate-class compressor or the tunnel cannot shrink it into a throughput that was never carried
(a long-window codec such as xz would still find the 1 MiB period; nothing on this path runs one).
The block's PRNG seed is an argument (--seed N, or SPEEDTARGET_SEED), printed at start, so two targets serve the
same bytes and a run can say which content it moved. Stdlib only. Threaded, no logging, no rate limiting by design."""
import argparse
import os
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_SEED = 20260901
MAXB = 200 * 1024 * 1024
CHUNK = random.Random(DEFAULT_SEED).randbytes(1 << 20)      # module-wide: every server in one process serves one seed


def make_chunk(seed):
    return random.Random(seed).randbytes(1 << 20)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vpn-speedtarget"

    def log_message(self, *a):
        pass

    def _reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._reply(200, b"ok", "text/plain")
        if u.path != "/down":
            return self.send_error(404)
        try:
            n = int(parse_qs(u.query).get("bytes", ["25000000"])[0])
        except ValueError:
            n = 25000000
        n = max(0, min(n, MAXB))
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(n))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        left = n
        try:
            while left > 0:
                take = min(left, len(CHUNK))
                self.wfile.write(CHUNK[:take])
                left -= take
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if urlparse(self.path).path != "/up":
            return self.send_error(404)
        n = int(self.headers.get("Content-Length", "0") or 0)
        left = n
        try:
            while left > 0:
                d = self.rfile.read(min(left, 65536))
                if not d:
                    break
                left -= len(d)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self._reply(200, b'{"ok":true,"received":%d}' % (n - left), "application/json")


def serve(port, host="0.0.0.0", server=None, seed=DEFAULT_SEED):
    global CHUNK
    CHUNK = make_chunk(seed)
    srv = server or ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"speedtarget on {host}:{srv.server_address[1]} seed={seed}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8899")))
    ap.add_argument("--seed", type=int, default=int(os.environ.get("SPEEDTARGET_SEED", DEFAULT_SEED)))
    a = ap.parse_args()
    serve(a.port, seed=a.seed)
