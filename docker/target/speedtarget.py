#!/usr/bin/env python3
"""Minimal speed-test target for the Gnosis VPN load tests.

Exists because speed.cloudflare.com rate-limits by source IP, and every VPN
client emerging from one exit shares that IP -- after ~45 min of sustained
testing it returned 429 with Retry-After 3071 to ALL of them. A target we own
has no such limit and gives a symmetric upload endpoint.

  GET  /down?bytes=N   -> N bytes (default 25000000)
  POST /up             -> reads and discards the body, returns 200
  GET  /health         -> ok

Stdlib only. Threaded, no logging, no rate limiting by design.
"""
import os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CHUNK = b"\0" * 65536
MAXB = 200 * 1024 * 1024


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vpn-speedtarget"

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path != "/down":
            self.send_error(404)
            return
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
                self.wfile.write(CHUNK[:min(left, len(CHUNK))])
                left -= min(left, len(CHUNK))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if urlparse(self.path).path != "/up":
            self.send_error(404)
            return
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
        body = b'{"ok":true,"received":%d}' % (n - left)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8899"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    print(f"speedtarget on 0.0.0.0:{port}", flush=True)
    srv.serve_forever()
