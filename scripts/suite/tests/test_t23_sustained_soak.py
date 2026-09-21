"""T23-sustained-soak (gate): a 1.5 Mbit/s bidirectional call plus a transfer pair every INTERVAL s for DUR s,
sampling error counters, reconnects, worker RSS and client log growth. Pass: no reconnect, no unbounded RSS growth
(last < 2 x first), log rate < LOG_MB_MIN_MAX MB/min (the client writes ~70 MB/min at hopr_transport::path=debug;
the gate exists for logging hot loops, 1.6 GB/min was the incident), call loss < CALL_LOSS_MAX % (T06's LOSS_MAX
for the same probe: a soak that delivers 24 % of its call must not pass; a missing report counts as 100 %).
The soak must outlive the deadman: deadman_cover raises it above DUR."""
import time

from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import num
from suitelib.target import curl_down, curl_up

TEST = "T23-sustained-soak"
KIND = "gate"
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
