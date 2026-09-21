"""The target services and the probes, end to end on loopback, no container and no tunnel: each server starts in a
thread on an ephemeral port, the matching probe runs as the subprocess the suite would start inside the client
(with --iface '' so it does not bind to a tunnel interface), and its report must show a complete, lossless run.
This is the protocol contract the live tests rely on (T06, T10, T11, T13, T18, T20, T23, T24)."""
import json
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

SUITE = Path(__file__).resolve().parent.parent
TARGET = SUITE.parent.parent / "docker" / "target"
PROBES = SUITE / "probes"
sys.path.insert(0, str(TARGET))
import callecho  # noqa: E402
import callsrv  # noqa: E402
import speedtarget  # noqa: E402
import streamsrv  # noqa: E402
from udpserver import bind_udp  # noqa: E402


def _udp_service(serve, **kw):
    s = bind_udp(0, "127.0.0.1")
    threading.Thread(target=serve, kwargs={"port": s.getsockname()[1], "sock": s, **kw}, daemon=True).start()
    return s.getsockname()[1]


def _probe(name, out, timeout=60, **args):
    cmd = [sys.executable, str(PROBES / f"{name}.py"), "--out", str(out)]
    for k, v in args.items():
        if v is True:
            cmd.append(f"--{k.replace('_', '-')}")
        else:
            cmd += [f"--{k.replace('_', '-')}", str(v)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def echo_port():
    return _udp_service(callecho.serve)


@pytest.fixture(scope="module")
def stream_port():
    return _udp_service(streamsrv.serve)


@pytest.fixture(scope="module")
def call_port(tmp_path_factory):
    return _udp_service(callsrv.serve, logdir=str(tmp_path_factory.mktemp("callsrv")))


@pytest.fixture(scope="module")
def http_port():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), speedtarget.Handler)
    threading.Thread(target=speedtarget.serve, kwargs={"port": 0, "host": "127.0.0.1", "server": srv}, daemon=True).start()
    return srv.server_address[1]


def test_echo_probe_round_trip(echo_port, tmp_path):
    j = _probe("relprobe", tmp_path / "echo", host="127.0.0.1", port=echo_port, rate_mbit=1.0, duration=2, size=600, iface="")
    assert j["sent"] > 150 and j["recv"] == j["sent"] and j["loss_pct"] == 0.0
    assert j["stalls_gt_1s"] == 0 and j["rebinds"] == 0 and j["rtt_ms"]["p99"] < 100
    assert (tmp_path / "echo.csv").read_text().startswith("t,recv,rtt_p50_ms,rtt_max_ms")


def test_stream_upload_gets_a_server_report(stream_port, tmp_path):
    j = _probe("streamprobe", tmp_path / "ul.json", mode="ul", host="127.0.0.1", port=stream_port, rate_mbit=1.0, duration=2, size=600, iface="")
    assert j["server_report"] is not None and j["server_report"]["recv"] == j["sent"]
    assert j["loss_pct"] == 0.0 and j["stalls_gt_1s"] == 0 and j["outage_total_s"] is None


def test_stream_download_measures_here(stream_port, tmp_path):
    j = _probe("streamprobe", tmp_path / "dl.json", mode="dl", host="127.0.0.1", port=stream_port, rate_mbit=1.0, duration=2, size=600, iface="")
    assert "end_marker" not in j and j["recv"] == j["sent"] > 150 and j["loss_pct"] == 0.0
    assert j["delay_over_min_ms"]["p99"] < 100 and j["stalls_gt_1s"] == 0


def test_call_probe_both_legs_and_server_counts(call_port, tmp_path):
    j = _probe("callprobe", tmp_path / "call", host="127.0.0.1", port=call_port, sid=4242, duration=2, iface="")
    assert j["server"] and j["server"]["sid"] == 4242
    assert j["up_loss_pct"]["video"] == 0.0 and j["up_loss_pct"]["audio"] == 0.0
    assert j["down_loss_pct"]["video"] < 5 and j["video"]["recv"] > 200 and j["rebinds"] == 0
    lines = (tmp_path / "call.csv").read_text().splitlines()
    assert any(l.startswith("S,0,") for l in lines) and any(l.startswith("R,0,") for l in lines) and "E," in lines[0]


def test_speedtarget_down_up_health(http_port):
    import urllib.request
    base = f"http://127.0.0.1:{http_port}"
    assert urllib.request.urlopen(f"{base}/health", timeout=5).read() == b"ok"
    body = urllib.request.urlopen(f"{base}/down?bytes=3000000", timeout=10).read()
    assert len(body) == 3000000 and len(set(body[:4096])) > 200      # sized, and not compressible zeros
    req = urllib.request.Request(f"{base}/up", data=b"z" * 500000, method="POST", headers={"Content-Type": "application/octet-stream"})
    assert json.loads(urllib.request.urlopen(req, timeout=10).read())["received"] == 500000


def test_unbound_socket_when_iface_empty():
    sys.path.insert(0, str(PROBES))
    import probelib
    ts = probelib.TunnelSocket("", 0)
    assert ts.ifindex == 0 and ts.port > 0 and ts.watch() is None
    ts.finish()
    assert ts.summary()["rebinds"] == 0 and ts.summary()["outages"] == []
    assert probelib.pct_ms([0.1, 0.2, 0.3], 0.5) == 200.0 and probelib.over_min([0.3, 0.1]) == pytest.approx([0.2, 0.0])
    assert probelib.stall_stats([(1.0, 6.0), (2.0, 1.5)])["stalls_gt_5s"] == 1


def test_main_runs_all_four_services(monkeypatch):
    """main.py binds the four services; it must exit non-zero when one dies (the container restarts)."""
    import main
    import time
    monkeypatch.setitem(main.SERVICES, "speedtarget", lambda: None)   # a service that returns = a service that died
    for k in ("callecho", "streamsrv", "callsrv"):
        monkeypatch.setitem(main.SERVICES, k, lambda: time.sleep(30))
    with pytest.raises(SystemExit) as e:
        main.main()
    assert e.value.code == 1
