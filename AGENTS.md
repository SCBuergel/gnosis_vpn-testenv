# AGENTS.md — rules for the regression suite

Read before changing `scripts/suite/`. Each rule is one line: what to do, then why (the incident that taught it). The catalogue is `docs/regression-catalogue.md`; the mechanics are `scripts/suite/README.md`.

## Shape of the run

- One run, one order: `just suite` runs t01 … t24 in file order every time. No profiles: a profile you can choose is a profile someone forgets to choose. Shorten with `--very-fast`, `--fast`, `--only`/`--skip`, or a per-test knob `--knob T<NN>_<VAR>=…` (beats `--very-fast`).
- Order is load-bearing: T03-repeatability-baseline runs third (repeatability on record before any gate reads a number), T05-loaded-latency fifth and T06-realtime-udp sixth (loaded latency and real-time loss are only comparable before an hour of load).
- A test is a module in `scripts/suite/tests/`; it runs by being there. Only `KIND = "runbook"` keeps it out of the run. Under the old list-based runner T19-background-load and T20-fault-injection sat in no list for a day and never ran.
- Three kinds: a *gate* may fail the run and must pass on a healthy stack; a *diagnostic* records and never fails (its FAIL becomes WARN); a *runbook* item is kept but in no run.
- One name everywhere: `T04-fixed-throughput` in prose and as the verdict/row label. The bare number survives only in the file name `test_t04_*.py` and the knob prefix `T04_`.
- `--very-fast` (15 s arms) cannot see anything that needs a long session, e.g. a reconnect cycle on the order of a minute (the liveness-ping artifact took ~85 s). Never conclude "healthy" from it; `--fast` keeps every sustained arm at 90 s or more.
- Every test has a timeout (`TEST_TIMEOUT`, or the module's `TIMEOUT(knobs)`); every subprocess call has one.

## Scoring

- Every threshold is absolute and named (`DOWN_MIN_MBIT=7`, `DOWN_P95_MAX_MS=1500`, …), calibrated on the reference stack and listed in the catalogue's threshold table. Nothing is scored against a previous run: that design ratcheted, mixed run modes, and once let a delivery collapse pass inside a ±279 % band.
- Read every threshold verdict on a run T03-repeatability-baseline flags UNSTABLE as weak evidence.
- A measurement needs a sample: a probe on a dead session sends a handful of packets and still prints a confident percentage (255 of 4688 sent, "60.78 % loss"). T06-realtime-udp fails an arm as UNMEASURED below `SAMPLE_MIN_PCT`; do the same in any new probe.
- Arms that share a session are not comparable (82.7 % loss on one arm, 0.41 % on the next). Each T06-realtime-udp arm connects for itself.
- XFAIL is a hard verdict with a tight mechanism, never a way to silence a test; it must report XPASS when the defect vanishes. An XFAIL bound from a number you cannot justify is worse than a plain gate (one was removed from T06-realtime-udp for that).
- Binary is not the goal, discrimination is: a gate whose verdict is the same on a broken stack manufactures confidence. T12-balancer-sweep's masking cell is n=1 per tier; check sensitivity before trusting its PASS.
- Every reconnect count is reported next to the tunnel-ping timeout count (`ping_timeouts` in `log_errors`). Three timeouts per reconnect means the liveness ping is failing; read that before reading load.

## Measuring

- Do not idle before measuring: `Client.connect()` floors the post-connect idle at `SURB_RAMP_WAIT` (25 s) and it must not be raised. A 75 s default made the return-path SURBs expire during the idle, so the first transfer starved the return path, the tunnel ping timed out and the watchdog reconnected: 8/8 first transfers failed on both hoprd 4.1.2 and 60269a3, 6/6 passed at a short wait. A full day of false "hoprd regression" findings.
- Ramp length is client-specific (0.96.2: 20 s; older branches: 60 s) and governs download throughput (60 s ramp ≈ 5 Mbit/s, 20 s ≈ 10). If a client needs longer, shorten the ramp (`GNOSISVPN_SURB_RAMP_SECS`, `[connection.surb_balancing.ramp]`); never idle longer. Only T07-cold-start's cold arm and T15-warmup-knee pass `ramp_wait_opt_out=True`, because measuring the ramp is their job.
- The deadman: every connect arms a detached `sleep DEADMAN && disconnect` (900 s) so the kill switch can never strand a host. A session that must outlive it calls `client.deadman_cover(DUR)` before connect (T23-sustained-soak, T24-sustained-upload). Without it the deadman disconnected the client at +15 min, the soak counted the rest as loss (24 % delivered = 900/3600) and still passed, and T24-sustained-upload never got its server report.
- The disarm kills the shell first, then the sleep, and the shell uses `&&`. The old order let the shell fall through to the disconnect: every connect made from a subshell disconnected its own client ~30 s later (T22-concurrent-clients read zero bytes on five of seven clients with a healthy tunnel ping). When a rung reads zero bytes on a live tunnel, grep the client log for `command=Disconnect` first. An `IFDOWN` in a probe with zero reconnects means the deadman, not the stack.
- Saved client logs drop the DEBUG path-planner lines (`SAVE_LOG_RAW=1` keeps them): one run saved 30 GB of them and the next died with the disk full.
- T22-concurrent-clients's ladder gate is one-sided (a higher rung may not fall more than `TOL_PCT` below a lower one): aggregate rising with concurrency is the healthy shape (10.6, 13.7, 16.1 Mbit/s at 1, 2, 4 clients).
- The probes bind to the tunnel interface and re-bind when it is recreated; a socket bound to a removed interface goes blind silently and reads as loss. Rebinds and outage seconds are reported next to loss, never inside it.

## Running on a host

- Never `cargo build` on the host while a measurement runs: eight vCPUs are shared by the exit, two relays, the client and the target.
- Never edit `scripts/suite/` under a running suite: the run's provenance becomes ambiguous.
- A run id is a directory and every test appends to it; the runner refuses an id that already holds results. Two runs once shared an id and their verdicts blended.
- Launch long runs as `systemd-run … -p KillMode=process` so the detached localcluster survives the wrapper. Consequence: `systemctl stop` reports the unit inactive while `just suite` and its pytest keep running; check with `ps` and kill by PID.
- Never `pkill -f` a pattern that appears in your own command line; it kills your SSH session. Anchor the pattern or kill by PID.
- The metrics collector must shed load: hoprd labels `hopr_surb_balancer_*` by `session_id`, sessions churn, and the series count only grows (52 → 118 in one run). `configs/otelcol.yaml` has a `memory_limiter` and a bounded queue; without them the collector reached 14 GB and the OOM killer took a running suite. The suite reads `/metrics` directly and never depends on the collector.
- Let the stack settle before measuring: client channels open on-chain asynchronously, and T01-topology-preconditions polls for them because a suite launched 15 s after `just up` failed preconditions on a healthy stack.
- `cluster-restart` rebuilds the chain and races a dying Anvil ("insufficient token balance at the signer"). Impairment tests apply latency live with `tc netem` on `lo`; `cluster-stop` waits for the chain container to be gone.
- Channel close is two-phase with a grace period: a close/reopen inside one run leaves `PendingToClose` and the exit with no usable return relay, wedging every later test. T09-impairment-ladder is impairment-only; no test touches the channel API.

## Versions and builds

- hoprd 4.1.x: branch `release/4.1` (tag `v4.1.2` is on the 5.0 line and its localcluster writes a config 4.x rejects); 4.0.3: tag `v4.0.3`. Build the localcluster from the same tree as the hoprd binary.
- `HOPR_INTERNAL_IN_PACKET_PIPELINE_CONCURRENCY=64` is a no-op from `release/4.1 @ 60269a3` (it worked around hoprnet #8246 before); a run with it is not a different configuration. The #8425 pool arbiter has no config surface and made no difference in an A/B (6/6 vs 6/6).
- Client: `release/hoprdv4` is the 4.x line, `main` the 0.101/5.x line. The release configuration is the upstream image (`just build-client`); the glibc images are for bisecting cargo builds (`build-client-glibc DIR TAG REVISION` labels the image with the commit) and are not release evidence.
- The client routes RFC1918 around the tunnel, so the target lives on `198.18.0.0/24` behind an exit-side MASQUERADE; `_client-start` copies the identity into the writable state dir because a newer client migrates its keystore in place.

## Attribution

- A failure independent of every knob you turn (rate, direction, MTU) is the rig, not the product; read the earliest error in the log, not the loudest. The ~85 s reconnect cycle read as "load kills the tunnel" for two days was the client's liveness ping aimed at 10.128.0.1 while the server held 10.129.0.1 (`SERVER_PING_ALIAS` is the stopgap, T01-topology-preconditions checks it, T06-realtime-udp's idle arm measures it; details in the catalogue).
