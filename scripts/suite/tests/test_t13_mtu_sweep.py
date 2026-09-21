"""T13-mtu-sweep (gate): MTU is a mechanism switch, not a tuning knob: at <=940 B a datagram fits one HOPR packet so
no opportunistic second SURB is minted. Until 2026-09-20 this was an XFAIL tagged hoprnet#8392 (reconnects at
1420 and 1280, clean at 940); on hoprd 4.1.3 the overflow is gone, so this is the plain gate the XFAIL was
waiting to become: zero reconnects at every MTU. FIXED_BY names the mechanism for the day it comes back.
The 940 rung's download median is held against MTU940_DOWN_MIN_MBIT (measured 12.3 on the reference stack; 6 sits
under T04's 7 because a 940 B MTU carries about a third more packets per byte)."""
from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.target import summary_row, transfer_series

TEST = "T13-mtu-sweep"
KIND = "gate"
KNOBS = dict(MTUS="1420 1280 940", STREAM_S=q(120, 120), FIXED_BY="hoprnet#8392", MTU940_DOWN_MIN_MBIT=6)
TIMEOUT = lambda k: len(k.words("MTUS")) * (k.STREAM_S + 1200)   # seconds; the harness fails the test past this


def test_mtu_sweep(cfg, run, client, target, checks, knobs):
    k = knobs
    rec, m940 = {}, 0
    for mtu in k.words("MTUS"):
        s = connect_or_fail(checks, client, cfg.dest, 15, label=f"mtu {mtu}")
        if not s:
            return
        with s:
            if mtu != "default":
                client.exec(f"ip link set dev {s.iface} mtu {mtu}")
            eff = client.out(f"cat /sys/class/net/{s.iface}/mtu")
            summ = transfer_series(checks, client, f"t13-{mtu}", target.ip, cfg.q(cfg.reps, 2), cfg.bytes, cfg.cap)
            client.probe("streamprobe", f"t13-{mtu}.json", timeout=k.STREAM_S + 90, mode="ul", host=target.ip, port=target.stream_port,
                         rate_mbit=3, duration=k.STREAM_S, size=1200, iface=s.iface)
            j = run.read_json(f"t13-{mtu}.json", {})
            e = s.errors()
        rec[mtu] = e["reconnects"]
        if mtu == "940":
            m940 = summ["down_median"]
        checks.row(mtu=mtu, effective_mtu=eff, summary=summary_row(summ), stream=j, errors=e)
        checks.record(f"mtu {eff}: down {summ['down_median']} up {summ['up_median']} Mbit/s, stream loss {j.get('loss_pct')}%, "
                      f"reconnects {rec[mtu]} (tunnel-ping timeouts {e['ping_timeouts']})")
    if m940:
        checks.assert_min("download median at mtu 940", m940, "Mbit/s", "MTU940_DOWN_MIN_MBIT", k.MTU940_DOWN_MIN_MBIT)
    big = rec.get("1420", 0) + rec.get("1280", 0)
    small = rec.get("940", 0)
    if small == 0 and big == 0:
        checks.passed(f"no reconnect at any mtu [{k.MTUS}]")
    elif small == 0:
        checks.failed(f"organic-SURB overflow is back ({k.FIXED_BY}): reconnects at 1420/1280 ({rec.get('1420', 0)}/{rec.get('1280', 0)}), clean at 940")
    else:
        checks.failed(f"reconnects 1420={rec.get('1420', 0)} 1280={rec.get('1280', 0)} 940={small}; 940 must be clean, so this is not the known mechanism")
