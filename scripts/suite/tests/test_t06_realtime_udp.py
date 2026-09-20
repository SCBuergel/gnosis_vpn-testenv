"""T06-realtime-udp (gate): behaviour under constant bitrate. Arms, each on its own session: one long
bidirectional echo call (relprobe) at ECHO_RATE for ECHO_DUR s, which is long enough to show a reconnect cycle;
upload-only and download-only streams (streamprobe) at STREAM_RATE for STREAM_DUR s, which discriminate the
direction; and the download stream again on a session that has just carried bulk transfers (AFTER_BULK).

Every arm is binary: no reconnect during the arm, loss below LOSS_MAX %, no stall over STALL_MAX s, and the
probe must actually have sent what it set out to send. A reconnect is a hard failure on its own, and the verdict
names the tunnel-ping timeouts that preceded it (three per reconnect means the liveness ping itself is failing).
The sample guard is the point: a probe on a broken session sends a handful of packets and still prints a
confident loss percentage (255 of 4688 sent, "60.78 % loss"); below SAMPLE_MIN_PCT of the expected count the arm
is UNMEASURED. Each arm gets its own session because arms sharing one were order-dependent (82.7 % then 0.41 %).
There is deliberately no XFAIL here: an earlier bound came from a number contaminated by the SURB ramp."""
from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import num
from suitelib.target import transfer_series

TEST = "T06-realtime-udp"
KIND = "gate"
KNOBS = dict(ECHO_RATE=1.5, ECHO_DUR=q(300, 120), STREAM_RATE=3.0, STREAM_DUR=q(120, 90), LOSS_MAX=5, STALL_MAX=5,
             SIZE=1200, SAMPLE_MIN_PCT=80, PER_ARM_SESSION=1, AFTER_BULK=1)


def expected_pkts(rate_mbit, dur, size):
    return int(float(rate_mbit) * 1e6 / 8 / float(size) * float(dur))


def check_arm(checks, k, label, rate, dur, j, e):
    """One arm's verdict from the probe JSON j and the log error counters e."""
    loss = j.get("loss_pct")
    stall = int(j.get("stalls_gt_5s") or 0)
    sent = int(j.get("sent") or 0)
    worst = (j.get("worst_stalls") or [[0, 0]])[0][1] if j.get("worst_stalls") else 0
    p99 = (j.get("delay_over_min_ms") or j.get("rtt_ms") or {}).get("p99")
    exp = expected_pkts(rate, dur, k.SIZE)
    rec = int(e.get("reconnects") or 0)
    rebinds = int(j.get("rebinds") or 0)
    outage = j.get("outage_total_s")
    if outage is None:
        outage = "n/a (upload: see the server report's stalls)"
    pct = round(sent * 100.0 / max(exp, 1), 1)
    checks.row(label=label, rate_mbit=rate, sent_pkts=sent, expected_pkts=exp, sent_pct=pct, result=j, errors=e)
    # 1. did the tunnel survive the arm? A reconnect is the defect itself; it fails the arm before any loss figure is read.
    if rec > 0:
        return checks.failed(f"{label}: RECONNECT during the arm - client reconnects {rec} after {e.get('ping_timeouts')} tunnel-ping "
                             f"timeouts, probe rebinds {rebinds}, outage {outage}s; loss on the live path {loss}%, sample {pct}% of expected")
    # 2. did the probe run at all?
    if pct < k.SAMPLE_MIN_PCT:
        return checks.failed(f"{label}: UNMEASURED - probe sent {sent} of {exp} expected packets ({pct}%, floor {k.SAMPLE_MIN_PCT}%); "
                             f"any loss figure from this arm is meaningless (reported {loss}%)")
    # 3. did it report anything?
    if loss is None:
        return checks.failed(f"{label}: no loss figure - the probe's report never arrived (sent {sent} packets in {j.get('duration_s')} s)")
    # 4. the actual assertions
    why = []
    if num(loss) >= k.LOSS_MAX:
        why.append(f"loss {loss}% >= {k.LOSS_MAX}%")
    if stall > 0:
        why.append(f"{stall} stall(s) over {k.STALL_MAX}s, worst {worst}s")
    if not why:
        return checks.passed(f"{label}: loss {loss}%, worst stall {worst}s, p99 {p99} ms, sample {pct}% of expected, no reconnect")
    return checks.failed(f"{label}: {'; '.join(why)} (p99 {p99} ms, sample {pct}% of expected, no reconnect)")


def test_realtime_udp(cfg, run, client, cluster, target, checks, knobs):
    k = knobs
    shared = {"s": None}

    def arm_connect():
        if k.PER_ARM_SESSION == 0 and shared["s"] is not None:
            return shared["s"]
        shared["s"] = connect_or_fail(checks, client, cfg.dest, 0)
        return shared["s"]

    def arm_disconnect():
        if k.PER_ARM_SESSION != 0 and shared["s"] is not None:
            client.disconnect()
            shared["s"] = None

    def read(name):
        return run.read_json(name, {"loss_pct": 100, "sent": 0})

    with cluster.sampler(run, "t06", 1, [client.name, cfg.server]):
        # arm 1 - the gate: one long bidirectional call at the realistic rate
        r = k.ECHO_RATE
        s = arm_connect()
        if s:
            client.probe("relprobe", f"t06-echo-{r}", timeout=k.ECHO_DUR + 90, host=target.ip, port=target.echo_port,
                         rate_mbit=r, duration=k.ECHO_DUR, size=k.SIZE, iface=s.iface)
            check_arm(checks, k, f"echo-{r}Mbit", r, k.ECHO_DUR, read(f"t06-echo-{r}.json"), s.errors())
            s.save_log(f"t06-echo-{r}")
            arm_disconnect()
        else:
            checks.failed(f"echo-{r}Mbit: connect failed")
        # arms 2 and 3 - the direction discriminators: uploads ride the forward path, downloads the SURB-metered return path
        r = k.STREAM_RATE
        for mode in ("ul", "dl"):
            s = arm_connect()
            if not s:
                checks.failed(f"{mode}-{r}Mbit: connect failed")
                continue
            client.probe("streamprobe", f"t06-{mode}-{r}.json", timeout=k.STREAM_DUR + 90, mode=mode, host=target.ip,
                         port=target.stream_port, rate_mbit=r, duration=k.STREAM_DUR, size=k.SIZE, iface=s.iface)
            check_arm(checks, k, f"{mode}-{r}Mbit", r, k.STREAM_DUR, read(f"t06-{mode}-{r}.json"), s.errors())
            s.save_log(f"t06-{mode}-{r}")
            arm_disconnect()
        # arm 4 - the download stream on a session that has just carried bulk transfers. T09 used to run its stream
        # this way and every unimpaired cell failed on it (9-54 reassembly failures, 16-71 % loss) while the
        # fresh-session arm read 0.2 % loss; whatever the bulk transfers leave on the session is the defect this isolates.
        if k.AFTER_BULK == 1:
            s = arm_connect()
            if s:
                transfer_series(checks, client, "t06-bulk", target.ip, cfg.q(cfg.reps, 1), cfg.bytes, cfg.cap)
                client.probe("streamprobe", f"t06-dl-after-bulk-{r}.json", timeout=k.STREAM_DUR + 90, mode="dl", host=target.ip,
                             port=target.stream_port, rate_mbit=r, duration=k.STREAM_DUR, size=k.SIZE, iface=s.iface)
                check_arm(checks, k, f"dl-after-bulk-{r}Mbit", r, k.STREAM_DUR, read(f"t06-dl-after-bulk-{r}.json"), s.errors())
                s.save_log(f"t06-dl-after-bulk-{r}")
                arm_disconnect()
            else:
                checks.failed(f"dl-after-bulk-{r}Mbit: connect failed")
    errs = shared["s"].errors() if shared["s"] else {}
    if shared["s"]:
        shared["s"].save_log("t06")
        client.disconnect()
    checks.row(kind="summary", errors=errs, echo_rate=k.ECHO_RATE, echo_dur=k.ECHO_DUR, stream_rate=k.STREAM_RATE,
               stream_dur=k.STREAM_DUR, per_arm_session=k.PER_ARM_SESSION)
