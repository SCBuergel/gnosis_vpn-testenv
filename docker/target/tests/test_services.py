"""The target's own tests, on loopback, stdlib clients only: every service answers its wire protocol, the speed
target's content is sized, incompressible and seed-determined, and main.py exits when a service dies. The probes'
end-to-end contract (probe process against service) is tests/selftest/test_target_protocol.py."""
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import callecho
import main
import speedtarget
import streamsrv
from udpserver import bind_udp


def _udp_service(serve, **kw):
    s = bind_udp(0, "127.0.0.1")
    threading.Thread(target=serve, kwargs={"port": s.getsockname()[1], "sock": s, **kw}, daemon=True).start()
    return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_port():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), speedtarget.Handler)
    threading.Thread(target=speedtarget.serve, kwargs={"port": 0, "host": "127.0.0.1", "server": srv, "seed": 7}, daemon=True).start()
    return srv.server_address[1]


def _get(port, path, timeout=10):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
        return resp.read()


def test_speedtarget_health_down_up(http_port):
    assert _get(http_port, "/health") == b"ok"
    body = _get(http_port, "/down?bytes=3000000")
    assert len(body) == 3000000 and len(set(body[:4096])) > 200      # sized, and not compressible zeros
    req = urllib.request.Request(f"http://127.0.0.1:{http_port}/up", data=b"z" * 500000, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert json.loads(resp.read())["received"] == 500000


def test_speedtarget_sizes_default_and_cap(http_port):
    assert len(_get(http_port, "/down")) == 25000000
    assert len(_get(http_port, "/down?bytes=0")) == 0
    assert len(_get(http_port, "/down?bytes=x")) == 25000000            # unparsable size falls back to the default
    with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/down?bytes=999999999999", timeout=10) as big:
        assert int(big.headers["Content-Length"]) == speedtarget.MAXB


def test_speedtarget_content_follows_the_seed(http_port):
    served = _get(http_port, "/down?bytes=4096")
    assert served == speedtarget.make_chunk(7)[:4096]
    assert speedtarget.make_chunk(7) == speedtarget.make_chunk(7) and speedtarget.make_chunk(7) != speedtarget.make_chunk(8)
    # a 1 MiB period: byte i equals byte i + 1 MiB
    two = _get(http_port, f"/down?bytes={(1 << 20) + 16}")
    assert two[:16] == two[1 << 20:]


def test_speedtarget_unknown_paths_404(http_port):
    for method, path in (("GET", "/nope"), ("POST", "/down")):
        with pytest.raises(urllib.error.HTTPError) as e:
            with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{http_port}{path}", data=b"" if method == "POST" else None, method=method), timeout=5):
                pass
        assert e.value.code == 404


def test_callecho_echoes_datagrams():
    port = _udp_service(callecho.serve)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    for payload in (b"a" * 100, b"b" * 1200):
        s.sendto(payload, ("127.0.0.1", port))
        assert s.recv(2000) == payload
    s.close()


def test_streamsrv_counts_an_upload_and_streams_a_download():
    port = _udp_service(streamsrv.serve)
    srv = ("127.0.0.1", port)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    sid = 42
    for seq in range(20):                                             # UL: numbered packets, then ask for the report
        s.sendto(b"UPLD" + struct.pack("!IId", sid, seq, time.time()) + b"x" * 100, srv)
    time.sleep(0.2)
    s.sendto(b"UPRQ" + struct.pack("!IId", sid, 20, time.time()), srv)
    d = s.recv(65535)
    assert d[:4] == b"UPRP"
    rep = json.loads(d[4:])
    assert rep["recv"] == 20 and rep["sent"] == 20 and rep["loss_pct"] == 0.0
    s.sendto(b"CTLD" + struct.pack("!IfII", sid + 1, 50.0, 100, 1), srv)   # DL: 50 pps of 100 B for 1 s
    data, end = 0, None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and end is None:
        d = s.recv(65535)
        if d[:4] == b"DLDA":
            data += 1
        elif d[:4] == b"DLND":
            end = struct.unpack("!II", d[4:12])[1]
    assert end is not None and 40 <= data <= end <= 60
    time.sleep(2.5)                                                    # the DLND burst (8 x 0.25 s) ends, the sid is forgotten
    s.settimeout(0.3)
    try:
        while True:
            s.recv(65535)                                              # drain the rest of that burst
    except socket.timeout:
        pass
    s.settimeout(3)
    s.sendto(b"CTLD" + struct.pack("!IfII", sid + 1, 50.0, 100, 1), srv)   # the same sid streams again
    again = 0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        d = s.recv(65535)
        if d[:4] == b"DLDA":
            again += 1
        elif d[:4] == b"DLND":
            break
    assert again >= 40
    s.close()


def test_main_exits_when_a_service_dies(monkeypatch):
    """main.py binds the four services; it must exit non-zero when one dies (the container restarts)."""
    monkeypatch.setitem(main.SERVICES, "speedtarget", lambda: None)   # a service that returns = a service that died
    for k in ("callecho", "streamsrv", "callsrv"):
        monkeypatch.setitem(main.SERVICES, k, lambda: time.sleep(30))
    with pytest.raises(SystemExit) as e:
        main.main()
    assert e.value.code == 1
