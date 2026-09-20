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

Mechanism — MEASURED by the deep dive on 2026-09-20 (run `exploration/runs/20260920T081129Z-deepdive-disconnect`), and it is NOT SURB exhaustion:

- **SURB buffers stay full throughout.** At every ping timeout and at the reconnect, the client's active-session `hopr_session_surb_buffer_estimate` was ~14000-24000 and the exit's `hopr_surb_balancer_current_buffer_estimate` ~12500 (above its 9766 target). The earlier "ping reply has no SURB" guess is WRONG.
- **The download goodput undergoes congestion collapse.** The 15 Mbit/s download starts ~10 Mbit/s and then decays monotonically to ~0 over ~2 minutes, with 16591 `hopr_protocol_session::socket: failed to reassemble frame ... expired or discarded`. A CBR flow with no backpressure keeps the bottleneck queue full; frames sit past the reassembly deadline and are dropped whole; goodput spirals down. The periodic tunnel ping rides that same collapsed download path and eventually misses enough consecutive probes to trip the watchdog: `TunnelPingResult: Error(Ping timed out)` → `tunnel ping ... exceeded max failures - reconnecting` → `network link removed` → `created TUN device` ~3.5 s later. It is the liveness watchdog, NOT a data-plane crash (no `worker process exited`).
- **Direction asymmetry is the clincher.** The 15 Mbit/s UPLOAD control on the same stack sustained the full 15 Mbit/s the whole arm, with ZERO reassembly failures and NO reconnect (client SURBs merely piled up to ~297000, unused). Only the download (server→client) collapses. So the defect is in the server→client data plane — its reassembly / queueing under CBR overload — not a general capacity limit and not SURBs. TCP downloads survive because TCP backs off; the CBR flow has no backpressure.

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

- **Ran 2026-09-20 08:11 UTC** after a fresh stack restart. Results: `/root/testenv/gnosis_vpn-testenv/exploration/runs/20260920T081129Z-deepdive-disconnect/` with `sample-<arm>.csv`, `logs/<arm>.log`, `probe-<arm>.json`; the printed timeline is in `/root/testenv/exp/deepdive.log`.
- Re-run any time on a settled stack: `cd /root/testenv/gnosis_vpn-testenv && exploration/deepdive-disconnect.sh`.
- Re-analyze a captured arm: `exploration/analyze-deepdive.py <run_dir> download`.

### Result (2026-09-20)

Answered: SURB buffers are FULL at every ping timeout and at the reconnect (client ~14000-24000, exit ~12500 > target 9766), so it is neither SURB exhaustion nor a SURB-starved ping reply. The download goodput collapses (10 → ~0 Mbit/s over ~2 min, 16591 frames expired/discarded) and the co-located liveness ping degrades with it until the watchdog reconnects. The upload arm at the same 15 Mbit/s did NOT collapse or reconnect (0 reassembly failures, full rate). See the mechanism paragraph under phenomenon A.

### The open question now

Why does the server→client direction congestion-collapse under CBR overload while client→server does not? Candidates to chase next: the session reassembly timeout/window on the client vs the exit; the bottleneck queue location (which relay or the exit egress) and its depth (bufferbloat); whether the exit paces its send or blasts at the offered 15 Mbit/s. And separately: should the liveness watchdog be tolerant of a congested (but alive) data plane rather than tearing the tunnel down — a longer timeout or more misses under load, or a ping lane that is not behind bulk data.

## What to try next (ordered)

1. **Explain the download-only congestion collapse** (the deep dive's open question above). Instrument the mixnet path: where does the bottleneck queue sit (a relay, the exit egress) and how deep; measure per-hop latency growth during the download blast (add `tc -s qdisc` / queue depth sampling, or hoprd session/queue metrics) to confirm bufferbloat; compare the client-side vs exit-side session reassembly timeout/window that decides when a frame is "expired". The asymmetry (upload fine, download collapses) is the strongest lead — find what differs between the two directions on this 3-node localcluster.
2. **Decide if the reconnect is the right behaviour.** The watchdog tears the tunnel down when the ping misses under a congested-but-alive data plane. Test a more tolerant policy (longer ping timeout / more misses under load, or a ping lane not queued behind bulk data) and whether the download then rides out the overload instead of reconnecting. This is a gnosis_vpn-lib `tunnel_ping_loop` question.
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
