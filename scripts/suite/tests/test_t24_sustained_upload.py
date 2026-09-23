"""T24-sustained-upload (gate, the organic-SURB overflow guard): does a long steady upload survive, and can the
client keep opening the replies? An upload-only stream (streamprobe) at RATE Mbit/s for DUR s with 1200 B
datagrams, per MTU in MTUS (default, then 940 so a datagram fits one HOPR packet), reading the client's
hopr_packet_rejected_count{reason="undecodable"} before and after. The session outlives the deadman through
client.deadman_cover(DUR); under the default the client was disconnected ~30 s before the sender finished and both
arms printed a blank loss.

Pass iff, per MTU, stream loss < 5 %, reconnects = 0 and undecodable grew by fewer than 50. A missing end-of-stream
report from the server is an UNMEASURED FAIL naming the second the tunnel interface went down, if the probe saw
it. Not implemented: the 6 Mbit/s rate, 1 Hz sampling of the exit's SURB estimate, counting cause=Size opener
evictions and clamped_to warnings.

Why: uploads died at +405 s (3 Mbit/s) and +250 s (6 Mbit/s), every time. Each full-size packet mints an organic
SURB, the client's reply-opener store caps at 100 000 per pseudonym and evicts oldest, the exit spends SURBs
oldest-first, so after ~100k packets no reply the exit sends can be opened. 900 B datagrams ran two 800 MB uploads
clean, which is the verification; the fix lineage is hoprnet#8392. The test stays as the regression guard."""
from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import num

TEST = "T24-sustained-upload"
KIND = "gate"
KNOBS = dict(RATE=3, DUR=q(900, 240), MTUS="default 940")
TIMEOUT = lambda k: len(k.words("MTUS")) * (k.DUR + 600)   # seconds; the harness fails the test past this
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
