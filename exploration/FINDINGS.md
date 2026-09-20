# Exploration findings — can the Gnosis VPN tunnel be broken?

Date: 2026-09-20. Stack: client `gnosis_vpn-client:0.96.3-4c0c0e4c` (release/hoprdv4 @ 4c0c0e4c), hoprd 4.1.3 (release/4.1 @ 60269a3), server 0.7.0, 3-node localcluster on the 8-vCPU DigitalOcean host. The `SERVER_PING_ALIAS` (10.128.0.1) stopgap was in place throughout, so the liveness-ping artifact is neutralised and every reconnect below is real.

Method: adversarial scenarios in `exploration/explore.sh`, each classified BROKE (transport torn down: reconnect, interface recreated, session Disconnected, or reassembly/decap errors) vs OVERLOAD (loss with the tunnel intact — expected) vs CLEAN. Full per-run output under `exploration/runs/`.

## Bottom line

On a freshly restarted stack the tunnel is robust. The one input that breaks it in isolation is **extreme constant-bitrate overload**, and even that self-heals. The dramatic "soak death" seen mid-battery and the 75 % loss in the suite's T23 are **real but not reproducible on a fresh stack** — they need accumulated, long-lived stack state that I could not isolate. There is also a concrete **test gap** in T23.

## Finding 1 — CBR overload tears the tunnel down instead of shedding load (reproducible, self-healing)

A constant-bitrate UDP download above the path's ~10-13 Mbit/s capacity does not degrade gracefully; past a point the client reconnects.

| download rate | verdict | loss | reconnect | session-layer reassembly failures |
| --- | --- | --- | --- | --- |
| 6 Mbit/s | CLEAN | 0.45 % | 0 | 0 |
| 8 Mbit/s | CLEAN | 0.51 % | 0 | 0 |
| 10 Mbit/s | degraded | 25.5 % | 0 | 14210 |
| 12 Mbit/s | degraded | 52.2 % | 0 | 19918 |
| 15 Mbit/s | BROKE | 57-82 % | 1 | ~19000-30000 |

The reassembly failures are `hopr_protocol_session::socket: failed to reassemble frame … has expired or has been discarded`: at these rates segments are dropped or arrive too late and the frame never completes. At 15 Mbit/s the return path starves enough that the periodic tunnel ping times out three times and the watchdog reconnects (a ~25 s outage). TCP downloads at the same nominal rate complete, because TCP backs off; the constant-bitrate flow has no backpressure and drives the session past the point of graceful loss.

**Why it matters:** the expected behaviour under overload is dropped packets, not a torn-down tunnel. A reconnect throws away every in-flight session and costs ~25 s. This is the "overload that breaks the tunnel" case, and it is the candidate test below.

**But it self-heals.** After a 120 s 15 Mbit/s blast that reconnected, 10 minutes of normal 1.5 Mbit/s traffic was pristine (0.01 % loss). Repeated blasts (8 × [15 Mbit/s blast + 1.5 Mbit/s recover]) never accumulated: the session stayed up the whole time and recovery returned to 0.04 % by the last cycles. So a single or repeated overload episode does not leave a lasting scar.

## Finding 2 — sustained low-rate delivery collapse (real, NOT reproducible on a fresh stack)

Twice we saw a modest 1.5 Mbit/s return-path flow degrade catastrophically over tens of minutes:

- **Suite T23 (fullrun5), the full 1-hour soak:** verdict PASS, but the echo call delivered only 24 % (75.86 % loss) and the log held 10769 return-path warnings. The gate passed a tunnel dropping three-quarters of a steady stream.
- **Exploration `soak` (30 min), run after ~50 min of battery abuse:** 23 % loss, one reconnect, a single 329.7 s (5.5 min) outage, session DOWN at the end. The stack then refused new connects for ~40 min (the client could not complete the exit version check over a fresh session) until a full `just down/up` restart.

**This did not reproduce on a fresh stack.** A fresh 20-minute soak at 1.5 Mbit/s was CLEAN (0.0 % loss); the idle control was CLEAN; a single overload burst self-heals; repeated overload does not accumulate. The common factor in the two failures is a long-lived, heavily-churned stack (T23 ran at hour five of the suite; the exploration soak followed the whole battery). The cause is real but sits in accumulated stack state I could not isolate in the time available, so it is reported as observed, not explained. Do not attribute it to the 1.5 Mbit/s load — that load alone is clean.

**A trap to avoid:** the return-path warnings that look alarming — `return-path relayer diversity collapsed to a single relayer`, `evicting surb … Expired`, `evicting reply opener … Expired` — climb steadily even on a CLEAN stack delivering 100 %. They are background chatter, not a break signal. During the clean fresh 20-minute soak they still accumulated to hundreds of lines with 0.0 % loss. Read delivered fraction and reconnects, not these counts.

## What did NOT break the tunnel (all CLEAN, fresh stack)

- **Sustained 1.5 Mbit/s echo, 20 min**, and idle 12 min — 0.0 % loss.
- **Idle then resume**, idle 30/60/120/240 s before a transfer — no reconnect, <1 % loss. Idling does not break resume once the liveness ping is fixed.
- **MTU / fragmentation**, datagrams 1400 → 60000 B forcing heavy IP fragmentation — 0 reassembly failures. Fragmentation is handled cleanly; Finding 1's reassembly storm is about rate, not size.
- **Packet reorder + duplication** (`tc netem reorder 25% delay 20ms duplicate 1%`) — 58 % loss from the netem drops but 0 reassembly, 0 reconnect, session up: OVERLOAD, not a break. The decapsulator tolerates reordering.
- **Connect/disconnect churn**, 12 rapid cycles — no state leak, every cycle connected and transferred.
- **Burst/idle oscillation**, 8 × (15 s burst + 20 s idle) — no accumulation.
- **Bidirectional** 3 Mbit/s up+down for 120 s — 0 reconnects.

## Test gap in T23-sustained-soak

T23 gates on `reconnects == 0`, bounded RSS, and log rate < 200 MB/min. It measures the echo call's loss (`call loss %` in its message) but does **not** gate on it. That is why fullrun5's T23 passed at 75.86 % call loss. One-line fix: fail when the soak call's `loss_pct` exceeds a threshold (e.g. 10 %). This would have caught the delivery collapse without needing to reach the reconnect.

## Reproducing

```sh
cd /root/testenv/gnosis_vpn-testenv                 # suite host, stack up and idle
exploration/explore.sh overload-scar               # Finding 1: baseline clean, 15 Mbit blast reconnects, recovers
exploration/explore.sh pure-download RATES="8 10 12 15"   # the rate ladder of Finding 1
exploration/explore.sh surb-collapse MODE=load DUR=1200   # the return-path warnings are noise: this is CLEAN on a fresh stack
exploration/candidate-overload-teardown.sh         # candidate gate for Finding 1 (fails at 15 Mbit/s on this stack)
```
