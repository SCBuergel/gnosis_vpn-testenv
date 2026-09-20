"""T28-transport-ab (runbook): application-transport A/B (HTTP/1.1 vs HTTP/3). Documented negative; needs an
HTTP/3 target and a curl with h3. Skips unless the client's curl reports HTTP3 and TARGET_H3_PORT is set."""
TEST = "T28-transport-ab"
KIND = "runbook"
KNOBS = dict(TARGET_H3_PORT="")


def test_transport_ab(client, checks, knobs):
    if "HTTP3" not in client.out("curl --version") or not knobs.TARGET_H3_PORT:
        checks.skip("no HTTP/3-capable curl in the client image / no TARGET_H3_PORT (documented negative, see catalogue)")
    checks.skip("h3 arm not implemented in this revision")
