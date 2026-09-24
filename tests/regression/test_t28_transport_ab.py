"""T28-transport-ab (runbook, documented negative): does the application transport change tunnelled throughput?
HTTP/1.1 vs HTTP/3 over the same sessions. SKIP unless the client's curl reports HTTP3 and TARGET_H3_PORT is set;
the h3 arm is not implemented, so it skips either way.

Why: measured on 2026-09-11/12 and none of it is a lever: HTTP/3 downloads were half of HTTP/1.1 and its uploads
acked 4/40, 3 minutes idle made downloads slower in 30/40, kernel BBR vs CUBIC was a coin flip on downloads. The
SURB-metered return path sets download throughput regardless. Kept so the axis is not re-tested from scratch per
release. If the h3 arm is ever built: force --http3-only (a silent fallback to HTTP/2 measures nothing), never read
size_upload for a QUIC upload without a 200, and expect a QUIC handshake right after a capped download to fail
while the return path drains."""
TEST = "T28-transport-ab"
KIND = "runbook"
KNOBS = dict(TARGET_H3_PORT="")


def test_transport_ab(client, checks, knobs):
    if "HTTP3" not in client.out("curl --version") or not knobs.TARGET_H3_PORT:
        checks.skip("no HTTP/3-capable curl in the client image / no TARGET_H3_PORT (documented negative, see catalogue)")
    checks.skip("h3 arm not implemented in this revision")
