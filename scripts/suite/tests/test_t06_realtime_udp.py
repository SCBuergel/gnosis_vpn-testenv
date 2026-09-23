"""T06-realtime-udp (gate): can the tunnel carry a fixed-rate flow, as opposed to a bulk TCP transfer that hides
return-path loss behind retransmission? Five arms, each on its own session (PER_ARM_SESSION): an idle control that
sends nothing for ECHO_DUR s (IDLE_ARM); a bidirectional echo call (relprobe against the target's :8901) at
ECHO_RATE for ECHO_DUR s, long enough to show a reconnect cycle; an upload-only and a download-only paced stream
(streamprobe against :8902) at STREAM_RATE for STREAM_DUR s, which tell the directions apart; and the download
stream again on a session that has just carried REPS bulk transfers (AFTER_BULK). Each probe reports loss, delay
above its minimum, stalls, rebinds and outage time; the probes re-bind when the tunnel interface is recreated and
keep their port, so loss means loss on the live path, and the outage a reconnect caused is reported beside it,
not inside it.

Idle arm: FAIL on any reconnect, WARN on a tunnel-ping timeout without one. Loaded arms, checked in this order:
FAIL RECONNECT on any reconnect (the verdict names the count, the tunnel-ping timeouts before it, rebinds and
outage seconds; three timeouts per reconnect means the liveness ping itself is failing); FAIL UNMEASURED when the
probe sent under SAMPLE_MIN_PCT of the expected RATE*1e6/8/SIZE*duration packets (a probe on a dead session sends
a handful and still prints a confident percentage: 255 of 4688, "60.78 % loss"); FAIL on no loss figure; FAIL at
loss >= LOSS_MAX or a gap over STALL_MAX s; PASS otherwise.

Why: the fleet recorded 54-96 % loss at 1.5 Mbit/s on a path whose TCP throughput looked fine. Each arm has its own
session because arms that shared one were order-dependent (82.7 % then 0.41 %). The after-bulk arm isolates what
T09 stumbled on: a 3 Mbit/s stream straight after bulk transfers on hoprd 4.1.3 showed 9-54 reassembly failures.
Until 2026-09-19 there were six rate arms and every one reconnected at ~50 s whatever the rate: that was the
liveness ping aimed at an address the server did not hold (T01 checks it now), not load. No XFAIL here: the one
that was tried came from a number contaminated by the SURB ramp."""
import time

from suitelib.client import connect_or_fail
from suitelib.config import q
from suitelib.stats import num
from suitelib.target import transfer_series

TEST = "T06-realtime-udp"
KIND = "gate"
KNOBS = dict(ECHO_RATE=1.5, ECHO_DUR=q(300, 120), STREAM_RATE=3.0, STREAM_DUR=q(120, 90), LOSS_MAX=5, STALL_MAX=5,
             SIZE=1200, SAMPLE_MIN_PCT=80, PER_ARM_SESSION=1, AFTER_BULK=1, IDLE_ARM=1)
TIMEOUT = lambda k: 5 * (k.ECHO_DUR + 3 * k.STREAM_DUR) + 1800   # seconds; the harness fails the test past this


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
    send_failed = int(j.get("send_failed") or 0)
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
        return checks.failed(f"{label}: UNMEASURED - probe sent {sent} of {exp} expected packets ({pct}%, floor {k.SAMPLE_MIN_PCT}%; "
                             f"{send_failed} sends failed on a missing interface); any loss figure from this arm is meaningless (reported {loss}%)")
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
    shared, last_since = {"s": None}, {"v": None}

    def arm_connect():
        if k.PER_ARM_SESSION == 0 and shared["s"] is not None:
            return shared["s"]
        shared["s"] = connect_or_fail(checks, client, cfg.dest, 0)
        if shared["s"]:
            last_since["v"] = shared["s"].since
        return shared["s"]

    def arm_disconnect():
        if k.PER_ARM_SESSION != 0 and shared["s"] is not None:
            client.disconnect()
            shared["s"] = None

    def read(name):
        return run.read_json(name, {"loss_pct": 100, "sent": 0})

    with cluster.sampler(run, "t06", 1, [client.name, cfg.server]):
        # arm 0 - the control, black box: connect and do NOTHING for ECHO_DUR s. A session that reconnects with no
        # traffic at all is not a load defect, whatever the loaded arms then show; the liveness-ping artifact
        # (every session reconnecting every ~85 s, idle or loaded) was misread as one for two days because no arm
        # measured the idle session. Zero reconnects and zero tunnel-ping timeouts, nothing else is asked.
        if k.IDLE_ARM == 1:
            s = arm_connect()
            if s:
                time.sleep(k.ECHO_DUR)
                e = s.errors()
                checks.row(label="idle", rate_mbit=0, duration_s=k.ECHO_DUR, errors=e)
                if e["reconnects"] > 0:
                    checks.failed(f"idle: RECONNECT with no traffic - reconnects {e['reconnects']}, tunnel-ping timeouts "
                                  f"{e['ping_timeouts']} in {k.ECHO_DUR}s idle; the liveness ping or the session itself is "
                                  f"failing, so no loaded arm below measures load")
                elif e["ping_timeouts"] > 0:
                    checks.warn(f"idle: {e['ping_timeouts']} tunnel-ping timeout(s) in {k.ECHO_DUR}s with no traffic and no reconnect")
                else:
                    checks.passed(f"idle: {k.ECHO_DUR}s idle session, no reconnect, no tunnel-ping timeout")
                s.save_log("t06-idle")
                arm_disconnect()
            else:
                checks.failed("idle: connect failed")
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
    errs = client.log_errors(last_since["v"]) if last_since["v"] else {}
    if shared["s"]:
        shared["s"].save_log("t06")
        client.disconnect()
    checks.row(kind="summary", errors=errs, echo_rate=k.ECHO_RATE, echo_dur=k.ECHO_DUR, stream_rate=k.STREAM_RATE,
               stream_dur=k.STREAM_DUR, per_arm_session=k.PER_ARM_SESSION)
