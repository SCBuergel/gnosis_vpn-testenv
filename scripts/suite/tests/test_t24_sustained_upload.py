"""T24-sustained-upload (gate, organic-SURB overflow guard): upload-only stream at RATE Mbit/s for DUR s with
full-size datagrams, sampling the client's undecodable counter; then the same with MTU 940. Pass: stream
completes without reconnect, undecodable flat (< 50), loss < 5 %. The session must outlive the deadman
(deadman_cover); a missing end-of-stream report is an UNMEASURED FAIL that says so."""
from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import num

TEST = "T24-sustained-upload"
KIND = "gate"
KNOBS = dict(RATE=3, DUR=q(900, 240), MTUS="default 940")
UNDECODABLE = 'hopr_packet_rejected_count{reason="undecodable"}'


def test_sustained_upload(cfg, run, client, target, checks, knobs):
    k = knobs
    client.deadman_cover(k.DUR)
    for mtu in k.words("MTUS"):
        s = connect_or_fail(checks, client, cfg.dest, 15, label=f"mtu {mtu}")
        if not s:
            return
        with s:
            if mtu != "default":
                client.exec(f"ip link set dev {s.iface} mtu {mtu}")
            eff = client.out(f"cat /sys/class/net/{s.iface}/mtu")
            u0 = client.telemetry_metric(UNDECODABLE)
            client.probe("streamprobe", f"t24-{mtu}.json", timeout=k.DUR + 120, mode="ul", host=target.ip, port=target.stream_port,
                         rate_mbit=k.RATE, duration=k.DUR, size=1200, iface=s.iface)
            u1 = client.telemetry_metric(UNDECODABLE)
            e = s.errors()
            s.save_log(f"t24-{mtu}")
        j = run.read_json(f"t24-{mtu}.json", {"loss_pct": 100})
        loss, rec = j.get("loss_pct"), e["reconnects"]
        start = j.get("start") or 0
        ifdown = next((round(t - start) for t, ev in j.get("events", []) if ev == "IFDOWN"), None)
        checks.row(mtu=mtu, effective_mtu=eff, result=j, errors=e, undecodable_before=u0, undecodable_after=u1, deadman=client.deadman_s)
        du = int(u1 or 0) - int(u0 or 0)
        base = (f"mtu {eff}, {k.RATE} Mbit/s x {k.DUR}s (sent {j.get('sent', 0)} pkts): reconnects {rec} (tunnel-ping timeouts "
                f"{e['ping_timeouts']}), undecodable +{du}")
        down = f" (tunnel interface went down at +{ifdown}s of the arm)" if ifdown is not None else ""
        if loss is None or j.get("server_report") is None:
            checks.failed(f"UNMEASURED - {base}; the server's end-of-stream report never arrived{down}, so loss is unknown")
            continue
        msg = f"{base}, loss {loss}%, stalls>5s {j.get('stalls_gt_5s', 0)}" + (f", interface down at +{ifdown}s" if ifdown is not None else "")
        checks.verdict(num(loss, 100) < 5 and rec == 0 and du < 50, msg)
