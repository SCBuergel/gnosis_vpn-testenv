"""T23-sustained-soak (gate): does a stack that passes a 3-minute test still pass after an hour? A CALL_RATE Mbit/s
bidirectional call for DUR s plus one download and one upload of BYTES every INTERVAL s, sampling error counters,
reconnects, worker RSS and client log growth at each interval. The session outlives the deadman through
client.deadman_cover(DUR + INTERVAL + 2 * (CAP + 30)): under the 900 s default the client was disconnected at
+15 min, the call delivered 24 % (900/3600) and the verdict was still PASS, because a deadman disconnect is not a
reconnect.

FAIL UNMEASURED when the call sent under SAMPLE_MIN_PCT of its expected CALL_RATE*1e6/8/1200*DUR packets (a send
the kernel refused on a missing interface is send_failed, never sent or loss). Otherwise PASS iff reconnects = 0,
final worker RSS < 2 x initial + 200000 kB, client log growth < LOG_MB_MIN_MAX MB/min (the client writes ~70
MB/min at hopr_transport::path=debug; the gate is for hot loops, 1.6 GB/min in the incident that motivated it) and
call loss < CALL_LOSS_MAX (T06's bound for the same probe at the same rate; a missing report counts as 100 %).
Stalls and reassembly failures are recorded.

Why: the defect that started the investigation appeared ~30 minutes into a clean call as opener-cache starvation
and a reconnect loop; short tests were green throughout. A soak also caught a relay silently rejecting tickets for
2.5 hours, and the log-rate check is the only thing that catches a logging or discovery hot loop (a 0.94.1 client
wrote 1.6 GB/min and 34 GB in a few hours) before it takes the host down."""
import time

from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import num
from suitelib.target import curl_down, curl_up

TEST = "T23-sustained-soak"
KIND = "gate"
GROUP = "endurance"
KNOBS = dict(DUR=q(3600, 600), INTERVAL=300, LOG_MB_MIN_MAX=200, CALL_LOSS_MAX=5, CALL_RATE=1.5, SAMPLE_MIN_PCT=80)
TIMEOUT = lambda k: k.DUR + k.INTERVAL + 1800   # seconds; the harness fails the test past this


def test_sustained_soak(cfg, run, client, cluster, target, checks, knobs):
    k = knobs
    # the loop overshoots DUR by up to one INTERVAL plus a transfer pair, and the deadman counts from connect
    client.deadman_cover(k.DUR + k.INTERVAL + 2 * (cfg.cap + 30))
    s = connect_or_fail(checks, client, cfg.dest, 20)
    if not s:
        return
    with s:
        with cluster.sampler(run, "t23", 5, [client.name, cfg.server]):
            client.probe_bg("relprobe", "t23-call", host=target.ip, port=target.echo_port, rate_mbit=k.CALL_RATE, duration=k.DUR, size=1200, iface=s.iface)
            t0 = time.time()
            rss0, log0 = client.worker_rss_kb(), client.log_path_size()
            while time.time() - t0 < k.DUR:
                time.sleep(k.INTERVAL)
                r = curl_down(client, target.ip, cfg.bytes, cfg.cap)
                u = curl_up(client, target.ip, cfg.bytes, cfg.cap)
                rss, e = client.worker_rss_kb(), s.errors()
                minute = int((time.time() - t0) / 60)
                checks.row(minute=minute, down=r, up=u, worker_rss_kb=rss, errors=e)
                checks.log(f"soak +{minute}min: down {r['mbit']} up {u['mbit']} rss {rss}kB reconnects {e['reconnects']}")
            time.sleep(5)
        e = s.errors()
        rss1, log1 = client.worker_rss_kb(), client.log_path_size()
        s.save_log("t23")
    call = run.read_json("t23-call.json", {})
    # a reconnect can recreate the --rm client container, which replaces its log file: count from zero then
    delta = log1 - log0 if log1 >= log0 else log1
    lograte = round(delta / 1048576 / max(1, k.DUR / 60), 2)
    checks.row(kind="summary", errors=e, rss_first=rss0, rss_last=rss1, log_mb_per_min=lograte, call=call)
    closs = num(call.get("loss_pct"), 100)
    # a measurement needs a sample: a call whose session died early sends a handful of packets and still prints a loss figure
    expected = int(k.CALL_RATE * 1e6 / 8 / 1200 * k.DUR)
    sent = int(call.get("sent") or 0)
    pct = round(sent * 100.0 / max(expected, 1), 1)
    if pct < k.SAMPLE_MIN_PCT:
        checks.failed(f"UNMEASURED - the call sent {sent} of {expected} expected packets ({pct}%, floor {k.SAMPLE_MIN_PCT}%; "
                      f"{call.get('send_failed', 0)} sends failed on a missing interface); reconnects "
                      f"{e['reconnects']} (tunnel-ping timeouts {e['ping_timeouts']}); the loss figure ({call.get('loss_pct')}%) is not a measurement")
        return
    msg = (f"{k.DUR}s: reconnects {e['reconnects']} (tunnel-ping timeouts {e['ping_timeouts']}), reassembly {e['reassembly_failed']}, "
           f"rss {rss0}->{rss1} kB, log {lograte} MB/min, call loss {call.get('loss_pct')}% stalls>5s {call.get('stalls_gt_5s')}")
    ok = e["reconnects"] == 0 and rss1 < 2 * max(rss0, 1) + 200000 and lograte < k.LOG_MB_MIN_MAX and closs < k.CALL_LOSS_MAX
    checks.verdict(ok, msg)
