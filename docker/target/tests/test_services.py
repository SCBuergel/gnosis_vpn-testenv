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
import callsrv
import main
import speedtarget
import streamsrv
from udpserver import bind_udp, pct_ms


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


def test_pct_ms_nearest_rank():
    # the same nearest-rank line as tests/probes/probelib.py and tests/suitelib/stats.py: n=4 at p=0.5 is the 2nd smallest
    assert pct_ms([0.1, 0.2, 0.3, 0.4], 0.5) == 200.0 and pct_ms([0.1, 0.2, 0.3, 0.4, 0.5], 0.5) == 300.0
    assert pct_ms([0.1, 0.2, 0.3, 0.4], 0.95) == 400.0 and pct_ms([0.4, 0.1], 1.0) == 400.0 and pct_ms([], 0.5) is None


def test_callsrv_answers_cale_from_a_cache_and_reaps_the_session(tmp_path):
    """CALE twice gives the same report; once the downstream is done and the report is old enough the session is
    forgotten and its log closed; a CALU for that sid afterwards opens a fresh session with a new handle."""
    state = {}
    # a short age after CALE, a long stale age: the test must not be reaped as "no CALE, silent" while it waits
    port = _udp_service(callsrv.serve, logdir=str(tmp_path), reap_after=0.4, stale_after=30, state=state)
    srv = ("127.0.0.1", port)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    sid = 4242
    s.sendto(b"CALS" + callsrv.CALS.pack(sid, 0.3, 20.0, 60, 20.0, 40) + b"\x00", srv)   # 0.3 s downstream

    def recv_tag(tag):
        for _ in range(200):                                          # the downstream's CALD may arrive before the ack
            d = s.recv(65535)
            if d[:4] == tag:
                return d
        raise AssertionError("no %r within 200 datagrams" % tag)

    recv_tag(b"CALA")
    for seq in range(5):
        s.sendto(b"CALU" + callsrv.HDR.pack(sid, seq, 0, time.time()) + b"x" * 40, srv)
    handle = state["sessions"][sid]["log"]
    time.sleep(0.6)                                                   # the downstream is over
    reps = []
    for _ in range(2):
        s.sendto(b"CALE" + struct.pack("!I", sid), srv)
        reps.append(json.loads(recv_tag(b"CALR")[4:]))
    assert reps[0] == reps[1] and reps[0]["recv_video"] == 5 and reps[0]["sent_video"] > 0
    deadline = time.time() + 3
    while sid in state["sessions"] and time.time() < deadline:
        time.sleep(0.1)
    assert sid not in state["sessions"] and handle.closed
    s.sendto(b"CALU" + callsrv.HDR.pack(sid, 99, 1, time.time()) + b"y" * 20, srv)   # a late packet: a fresh session
    time.sleep(0.2)
    assert sid in state["sessions"] and state["sessions"][sid]["log"] is not handle and not state["sessions"][sid]["log"].closed
    s.close()


def test_callsrv_reaps_a_session_whose_probe_died_before_cale(tmp_path):
    """A session with upstream packets and no CALE (the probe died) is reaped once it has been silent stale_after."""
    state = {}
    port = _udp_service(callsrv.serve, logdir=str(tmp_path), reap_after=30, stale_after=0.4, state=state)
    srv = ("127.0.0.1", port)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sid = 4343
    for seq in range(3):
        s.sendto(b"CALU" + callsrv.HDR.pack(sid, seq, 0, time.time()) + b"x" * 40, srv)
    deadline = time.time() + 2
    while sid not in state.get("sessions", {}) and time.time() < deadline:
        time.sleep(0.05)
    handle = state["sessions"][sid]["log"]
    deadline = time.time() + 3
    while sid in state["sessions"] and time.time() < deadline:
        time.sleep(0.1)
    assert sid not in state["sessions"] and handle.closed
    s.close()
