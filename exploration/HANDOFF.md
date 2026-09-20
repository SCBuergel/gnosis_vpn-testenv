# Handoff — the Gnosis VPN disconnect exploration

Written 2026-09-20 so a later session (or person) can continue without this chat's context. Read `FINDINGS.md` first for the full battery; this file is the disconnect deep dive specifically, plus how to run everything and what to try next. Everything here is in `exploration/` (tracked in git, but `runs/` is gitignored and lives only on the suite host).

## The stack under test

- client `gnosis_vpn-client:0.96.3-4c0c0e4c` (gnosis_vpn-client `release/hoprdv4` @ 4c0c0e4c)
- hoprd 4.1.3 (`release/4.1` @ 60269a3), localcluster from the same tree
- server 0.7.0, 3-node localcluster (exit + 2 relays), single 8-vCPU DigitalOcean host `139.59.157.121`
- `SERVER_PING_ALIAS` (10.128.0.1) is set on the server's `wggvpn`, so the old liveness-ping artifact is neutralised and every reconnect below is real. Confirm with `docker exec gnosis_vpn-server-0 ip -4 -o addr show dev wggvpn` — both 10.129.0.1 and 10.128.0.1 must be present.

## The disconnect, as understood so far

There are two disconnect phenomena. Keep them separate.

### A. Overload disconnect — reproducible in isolation, self-healing

A constant-bitrate (CBR) UDP **download** above the path's ~10-13 Mbit/s capacity makes the client tear the tunnel down instead of just dropping packets:

| DL rate | reconnect | loss | session-layer reassembly failures |
| --- | --- | --- | --- |
| 6-8 Mbit/s | no | <1 % | 0 |
| 10 Mbit/s | no | 25 % | 14210 |
| 12 Mbit/s | no | 52 % | 19918 |
| 15 Mbit/s | **yes (1)** | 57-82 % | ~19000-30000 |

Mechanism, best current understanding (to be confirmed by the deep dive below): the download saturates the server→client direction and consumes the session's SURBs; the periodic tunnel-liveness ping's reply cannot get back (no SURB and/or head-of-line blocked behind download data), so it times out three times (10 s interval, 15 s timeout) and the watchdog logs `tunnel ping ... exceeded max failures - reconnecting`, removes the WireGuard interface and recreates it ~3-4 s later. The reassembly failures are `hopr_protocol_session::socket: failed to reassemble frame ... has expired or has been discarded`. TCP downloads at the same nominal rate complete because TCP backs off; the CBR flow has no backpressure.

It **self-heals**: after a 120 s 15 Mbit/s blast that reconnected, 10 min of normal 1.5 Mbit/s traffic was pristine (0.01 % loss), and 8 repeated blast+recover cycles never accumulated (session stayed up throughout). So a single or repeated overload burst leaves no lasting damage.

Reproduce: `exploration/explore.sh overload-scar` (baseline clean → 15 Mbit blast reconnects → recovery clean) or `exploration/candidate-overload-teardown.sh` (gate that FAILs on this stack).

### B. Sustained delivery collapse / "soak death" — real but NOT reproduced on a fresh stack

Observed twice: (1) suite **T23** in fullrun5, the full 1-hour 1.5 Mbit/s soak — verdict PASS but the echo call delivered only 24 % (75.86 % loss), 10769 return-path warnings in the log; (2) the exploration **soak** (30 min, run after ~50 min of battery abuse) — 23 % loss, one reconnect, a single 329.7 s (5.5 min) outage, session DOWN at the end, then the client refused new connects for ~40 min (fresh sessions could not complete the exit version check) until a full `just down/up` restart.

It did **not** reproduce on a fresh stack: a fresh 20-min soak at 1.5 Mbit/s was CLEAN (0.0 % loss), the idle control was CLEAN, a single overload burst self-heals, repeated overload does not accumulate. The common factor in the two failures is a long-lived, heavily-churned stack (T23 ran at hour five of the suite). Cause is accumulated stack state, not the modest load — do not blame the 1.5 Mbit/s flow.

**Trap:** `return-path relayer diversity collapsed to a single relayer`, `evicting surb ... Expired`, `evicting reply opener ... Expired` climb into the hundreds/thousands even on a CLEAN stack at 0 % loss. They are background noise, not a break signal. Read delivered fraction and reconnects.

## The deep dive (in flight / ready to re-run)

`exploration/deepdive-disconnect.sh` reproduces phenomenon A in isolation and instruments it to pin the mechanism. Three arms, each on its own fresh session, sampling every 1 s throughout:
- WireGuard rx/tx (download/upload progress),
- the client's active-session SURB buffer (`hopr_session_surb_buffer_estimate`, max over sessions),
- the exit's SURB balancer (`hopr_surb_balancer_current_buffer_estimate` / `_target` / `_rate`),

then lining those up against the millisecond ping/reconnect log via `exploration/analyze-deepdive.py`.

Arms: `baseline` (1.5 Mbit/s echo 30 s, must be clean), `download` (15 Mbit/s DL 180 s — the disconnect), `upload` (15 Mbit/s UL 180 s — direction control: does saturating the other way also disconnect?).

- **Launched 2026-09-20 08:04 UTC** as `systemd-run --unit=exp-deepdive` after a fresh stack restart. Log: `/root/testenv/exp/deepdive.log`. Results: `/root/testenv/gnosis_vpn-testenv/exploration/runs/<stamp>-deepdive-disconnect/` with `sample-<arm>.csv`, `logs/<arm>.log`, `probe-<arm>.json`, and the printed timeline in `deepdive.log`.
- Re-run any time on a settled stack: `cd /root/testenv/gnosis_vpn-testenv && exploration/deepdive-disconnect.sh`.
- Re-analyze a captured arm: `exploration/analyze-deepdive.py <run_dir> download`.

### The specific question the deep dive answers

At the moment the pings start timing out (t of `PING_TIMEOUT` events), what is `client_surb` and `exit_buf_est`?
- If `client_surb` → 0 at the ping failures: **SURB exhaustion** — the download drains the SURB pool so the ping reply has no return resource. Fix direction: reserve SURBs for the liveness ping, or give the ping its own low-rate lane.
- If `client_surb` stays > 0 but download rx keeps flowing through the ping window: **head-of-line blocking / queue congestion** — the ping reply is queued behind bulk download data past the 15 s timeout. Fix direction: prioritise the ping, or make the watchdog tolerant of congestion (longer timeout / more misses under load).
- If the `upload` arm does NOT reconnect while `download` does: confirms the trigger is return-path/reply starvation specific to server→client saturation.

## What to try next (ordered)

1. **Finish reading the deep dive** (above). Settle SURB-exhaustion vs HOL-blocking, and the download-vs-upload asymmetry. That is the core of "what exactly is going wrong."
2. **Confirm the watchdog is the disconnect trigger, not a crash.** In the download arm's client log, verify the sequence is `TunnelPingResult: Error(Ping timed out)` ×3 → `exceeded max failures - reconnecting` → `network link removed` → `created TUN device`, with no `worker process exited` / panic. If it is the watchdog, this is a liveness-ping-under-load policy problem, not a data-plane crash.
3. **Find the exact rate/duration threshold** for the reconnect (between 12 and 15 Mbit/s; and whether a longer 12 Mbit/s run eventually reconnects). `exploration/explore.sh pure-download RATES="12 13 14 15"`.
4. **Isolate phenomenon B (soak death).** It needs accumulated state. Candidate reproduction: on a fresh stack, run a long mixed session (e.g. hours, or repeated churn: many connect/disconnect + periodic overload) and watch for the non-self-healing state (new connects failing the exit version check). Instrument with the same sampler. Also check the exit hoprd node's own health/CPU and REST latency over the long run.
5. **Client fix to verify (for A):** the periodic liveness ping should not compete with bulk data for SURBs / queue. This is a gnosis_vpn-lib change (`tunnel_ping_loop` in `core/runner.rs`) plus possibly a hoprd SURB-reservation. When a fixed client build exists, re-run `candidate-overload-teardown.sh` and the deep dive.
6. **Suite gap to close (independent of all the above):** T23-sustained-soak measures `call loss %` but only gates on reconnects/RSS/log-rate, so it passed at 75.86 % loss. Add a delivered-fraction assertion (fail when soak-call `loss_pct` > ~10 %). File in `scripts/suite/t23.sh` around the final `verdict` line.

## Operational reference (suite host)

- SSH: `ssh -i ~/.ssh/do_probe_ed25519 -o IdentitiesOnly=yes root@139.59.157.121` (the default key is refused). See memory `do-suite-host-access`.
- Checkout `/root/testenv/gnosis_vpn-testenv`; hoprd tree `/root/testenv/hoprd-4arb`. env in `/root/testenv/env.sh`; always also export `CLIENT_IMAGE`, `HOPRD_BIN`, `LOCALCLUSTER_BIN` as the launch scripts do (see `/root/testenv/exp/deepdive.sh`).
- Launch long jobs detached: `systemd-run --unit=exp-<name> -p KillMode=process --collect bash -c '...'`. Note the trap: `systemctl stop`/unit-inactive does NOT stop the child scripts (KillMode=process) — kill leftover PIDs by PID, never `pkill -f` a pattern that is in your own command line (it kills your SSH). `/root/testenv/exp/stop-suite.sh` is the safe stopper.
- Sync code from the laptop: `tar cf - exploration/... | ssh ... 'cd /root/testenv/gnosis_vpn-testenv && tar xf -'` (no rsync on the host). `chmod +x` after — tar loses the bit and scripts fail with "Permission denied".
- **The probes write inside the client to a path that must exist there.** The client mounts `/root/testenv-runs` at `/suite-out`, which is NOT the exploration run dir, so exploration probes write to a client-local `/tmp/explore` and results are read from probe stdout. Do not point a probe `--out` at the exploration run dir expecting the file on the host.
- The client logs at debug (~70 MB/min). `docker logs --since/--until` a bounded window when scraping; grepping the whole `--since` each minute grows unboundedly.
- Restart for a clean baseline: `just down >/dev/null; just up-nobuild >/dev/null; sleep 180`. After a soak death the stack can refuse new connects until this restart.
- Don't run two traffic loads at once (single-host stack): one arm/session at a time, or results contaminate each other.

## Files

- `exploration/explore.sh` — 11 adversarial scenarios (overload-scar, overload-repeat, surb-collapse, pure-download, idle-resume, mtu-frag, reorder, churn, burst-idle, bidir, soak). BROKE/OVERLOAD/CLEAN classifier.
- `exploration/deepdive-disconnect.sh` + `sampler.py` + `analyze-deepdive.py` — the instrumented disconnect reproduction.
- `exploration/candidate-overload-teardown.sh` — candidate suite gate for phenomenon A.
- `exploration/FINDINGS.md` — the full battery write-up. `exploration/README.md` — scenario overview.
- `exploration/run-all.sh` — batch runner for the battery.
- `exploration/runs/` — per-run outputs (server only, gitignored).
