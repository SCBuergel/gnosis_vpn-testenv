# Regression test catalogue

The suite in `scripts/suite/` runs 24 tests against a `gnosis_vpn-testenv` stack (localcluster, exit server, client containers, in-cluster traffic target) and keeps 8 runbook entries for investigations. Each test's method, criteria and the incident it comes from are in its module docstring (`scripts/suite/tests/test_tNN_<name>.py`); this file is the index: kind, knobs, what passes. Rules for changing the suite are in [AGENTS.md](../AGENTS.md), mechanics in [scripts/suite/README.md](../scripts/suite/README.md).

## Running it

```sh
just suite                    # the one run: T01 … T24 in file order
just suite --fast             # shorter durations; every sustained arm stays ≥ 90 s
just suite --very-fast        # 15 s arms, 1-2 MiB transfers: smoke only, cannot see a reconnect cycle
just suite --only t04,t06     # T01 still runs first unless skipped explicitly
just suite --knob T23_DUR=150 # a per-test knob beats --very-fast; also as env T23_DUR=150
just test t10                 # one test, including runbook entries
just suite --client NAME --dest ID --target HOST --no-cluster   # a production exit; cluster-only checks skip
just suite-selftest           # offline: library, target services, probe contracts
```

Results go to `SUITE_OUT_DIR/<run-id>/`: `rows.jsonl` (every measurement), `verdicts.jsonl`, `summary.csv`, `provenance.json`, `console.log`, `junit.xml`, `logs/`, `samples/`. A run id that already holds results is refused.

## Kinds

| Kind | Meaning | Scoring |
| --- | --- | --- |
| **gate** | must pass on a healthy stack | PASS / FAIL; only a gate can fail a run |
| *diagnostic* | characterization, control or covariate | recorded; a FAIL becomes WARN |
| *runbook* | investigation or fleet tooling, in no run | `just test tNN` |

Order is load-bearing: T03-repeatability-baseline runs third (the stack's repeatability is on record before any gate reads a number), T05-loaded-latency fifth and T06-realtime-udp sixth (loaded latency and real-time loss are only comparable before an hour of load). A T01-topology-preconditions FAIL aborts the run.

**Common parameters** (`suitelib/config.py`; `--fast` values in parentheses): `BYTES`=10000000 (5000000; `--very-fast` 2000000), `CAP`=90 s (60; 30), `REPS`=3 (2; 1), `SURB_RAMP_WAIT`=25 s (the floor on every post-connect idle unless a test passes `ramp_wait_opt_out=True`; it must not be raised, a longer idle lets the return-path SURBs expire), `CONNECT_TIMEOUT`=240 s, `TEST_TIMEOUT`=7200 s (a module may compute its own `TIMEOUT(knobs)`), `DEADMAN`=900 s (a session that must last longer calls `client.deadman_cover(SECONDS)`), `DEST`=node-0, `CLUSTER_SIZE`=3. Every pass criterion below is what the script enforces; anything the design asked for but the code does not assert is "recorded".

## Thresholds

Every number a gate holds a measurement against is absolute and named; nothing is compared with a previous run (delta scoring ratcheted, mixed run modes and once passed a delivery collapse inside a ±279 % band). Calibrated on the reference stack (hoprd 4.1.3 `release/4.1`, client 0.96.3, server 0.7.0, one 8-vCPU host, 2026-09-20; the calibration runs used Ubuntu/glibc cargo builds of the client and server, and the upstream Alpine/Nix images of the same commits read the same T04 medians on 2026-09-24: 11.8 down, 13.0 up at 10 MiB) at full-length durations; a `--very-fast` run reads 20-40 % lower and its threshold verdicts are smoke-level. A run T03-repeatability-baseline flags UNSTABLE makes every threshold verdict weak evidence.

| Knob | Default | Used by | Reference stack measured | Why this value |
| --- | --- | --- | --- | --- |
| `DOWN_MIN_MBIT` | 7 Mbit/s | T04-fixed-throughput download median at `FLOOR_MIB`=10, or the largest size in the cycle under it (2 MiB at `--very-fast`) | 10.9 (stdev 0.45 over 10 warm 10 MB sessions, glibc build; 11.8 on the upstream image, 2026-09-24; no larger size is calibrated as a rate) | the 2026-09 relay regression halved throughput; 7 catches a halving and clears ±12 % host noise |
| `UP_MIN_MBIT` | 7 Mbit/s | T04-fixed-throughput upload median at `FLOOR_MIB`=10, or the largest size under it | 13.0 (stdev 0.69, glibc build; 13.0 on the upstream image) | a halving lands at 6.5 |
| `DOWN_P95_MAX_MS` | 1500 ms | T05-loaded-latency RTT p95 during a saturating download | 359, 537, 1076 ms over three runs | the fleet's bufferbloat finding was 2-4 s; 40 % over the worst healthy reading |
| `UP_P95_MAX_MS` | 2500 ms | T05-loaded-latency RTT p95 during a saturating upload | 1124, 1153, 1746 ms | a parallel upload on the fleet hit 8.9 s |
| `LOSS_MAX` | 5 % | T06-realtime-udp, every arm | 0.02-1.7 % | a call above 5 % loss is audibly broken; the fleet's defect read 54-96 % |
| `STALL_MAX` | 5 s | T06-realtime-udp, every arm | 0-1.04 s | a 5 s gap is a dropped call, not jitter |
| `SAMPLE_MIN_PCT` | 80 % | T06-realtime-udp, T23-sustained-soak probe sample guard | 100 % | a probe that sent less did not run; its loss figure is meaningless |
| `CALL_LOSS_MAX` | 5 % | T23-sustained-soak call | 0.0-0.09 % | same probe and rate as T06's echo arm |
| `RECOVER_MAX` | 90 s | T10-forced-reconnect first downstream packet after the peer removal | 72-83 s | the client needs three liveness-ping cycles (~75 s) to notice a removed peer |
| `MTU940_DOWN_MIN_MBIT` | 6 Mbit/s | T13-mtu-sweep download median at MTU 940 | 12.3 | under T04's floor because 940 B carries about a third more packets per byte |
| `AGG_MIN_MBIT` | 8 Mbit/s | T22-concurrent-clients aggregate at the top rung | 16.1 at n=4 | below one client's rate: fires only on a collapse |
| `TOL_PCT` | 25 % | T22-concurrent-clients drop from a lower rung to a higher one | aggregate rose 10.6 → 13.7 → 16.1 | outside ±12-18 % repeatability, inside a real collapse |
| `COLD_DECAP_MULT`, `COLD_DECAP_FLOOR` | 2, 5 | T07-cold-start cold-arm decapsulation errors vs the warm arm's | 0 in both arms | a cold session may see a few, not a burst |
| `SPLIT_TOL_PCT`, `SPLIT_MIN_PATHS` | 60 %, 1000 | T08-relay-attribution return-path skew at equal latency | 8-72 % skew | fails an 80/20 split and beyond; expected to fail on some healthy runs, kept as a signal by decision |
| `UNSTABLE_PCT` | 50 % | T03-repeatability-baseline flag | 12.1 % | above it the stack cannot repeat its own numbers |
| `LOG_MB_MIN_MAX` | 200 MB/min | T23-sustained-soak client log growth | 63-70 MB/min at path-planner debug | the incident was 1.6 GB/min |

## The run

### T01-topology-preconditions · **gate**

Cluster, channels, relay forwarding, client readiness, both liveness-ping targets, no leftover impairment. FAIL aborts the run. `FWD_TIMEOUT`=20 s, `READY_TIMEOUT`=300 s, `CLIENT_CHANNEL_TIMEOUT`=240 s, `PERIODIC_PING_TARGET`=10.128.0.1. PASS iff cluster `running`, every node `channels_open` with ≥ `CLUSTER_SIZE`−1 outgoing `Open` channels, a 1-hop session from node 0 through every other node within `FWD_TIMEOUT`, client worker online within 120 s, `DEST` Ready within `READY_TIMEOUT`, client channel within `CLIENT_CHANNEL_TIMEOUT`, both ping targets on the server's `wggvpn`, no `netem` qdisc. WARN: another destination not Ready, an armed timer. Effective config recorded.

### T02-build-provenance · **gate**

Versions, image digests, OCI revision labels and the compiled-in `/hopr/mix/<ver>` id of client worker and hoprd; writes `provenance.json`. PASS iff both ids are readable and equal; FAIL if they differ; WARN if one is unreadable. The exit server carries no id.

### T03-repeatability-baseline · *diagnostic*

`N`=10 (5; 3) unchanged warm cells of one download and one upload of `BYTES`. Recorded: medians, stdev, `mde_pct` = 2·1.96·stdev/mean/√`REPS`·100 (larger of the two directions); `UNSTABLE BASELINE` above `UNSTABLE_PCT`=50.

### T04-fixed-throughput · **gate**

One session; `REPS` cycles of download-then-upload at each of `SIZES_MIB`="1 10 50" MiB ("1 10"; `--very-fast` "1 2"), `CAP` each, per-second stall detection, client-log counters, undecodable telemetry delta. `WAIT_AFTER_CONNECT`=0 (floored to `SURB_RAMP_WAIT`). Three kinds of verdict line (one per size, one for the counters, two floors): per size PASS iff every transfer of that size completes both ways (the line names the medians and the longest zero-progress second); session counters PASS iff `reassembly_failed`=0 and `reconnects`=0; floors: the medians at `FLOOR_MIB`=10 ≥ `DOWN_MIN_MBIT`=7 / `UP_MIN_MBIT`=7. Discards, decap errors and the other sizes' medians recorded. Open finding on the reference stack (run r3t04, 2026-09-23): the 50 MiB download stalls after 25-30 MB (frame-discard burst, zero progress to the cap, tunnel-ping reconnect), so the full-length run fails the 50 MiB completion and the counters; `--fast` passes. Details in the module docstring.

### T05-loaded-latency · **gate**

In-tunnel RTT idle, under a saturating download and a saturating upload, `PHASE_S`=30 s (15; 8) each; then `PARALLEL`="1 3 6" ("1 3" very-fast) concurrent downloads of `BYTES`. PASS iff p95 ≤ `DOWN_P95_MAX_MS`=1500 and ≤ `UP_P95_MAX_MS`=2500, every parallel flow completes within `CAP`, and no rung's aggregate (bytes received over wall time) falls below 0.8 × the previous rung's.

### T06-realtime-udp · **gate**

Five arms, each on its own session: idle control for `ECHO_DUR`; echo call at `ECHO_RATE`=1.5 Mbit/s for `ECHO_DUR`=300 s (120; 15); upload-only and download-only streams at `STREAM_RATE`=3 Mbit/s for `STREAM_DUR`=120 s (90; 15); the download stream after `REPS` bulk transfers. `SIZE`=1200 B. Idle arm: FAIL on a reconnect, WARN on a ping timeout. Loaded arms, in order: FAIL RECONNECT on any reconnect; FAIL UNMEASURED under `SAMPLE_MIN_PCT`=80 % of expected packets; FAIL on no loss figure; FAIL at loss ≥ `LOSS_MAX`=5 % or a gap > `STALL_MAX`=5 s; PASS otherwise. Rebinds and outage seconds reported next to loss.

### T07-cold-start · **gate**

`REPS` (1) transfers straight after connect (ramp wait opted out) vs after `WARM`=25 s. PASS iff `reconnects`=0 in both arms, cold completions ≥ warm completions, cold decap errors ≤ max(`COLD_DECAP_FLOOR`=5, `COLD_DECAP_MULT`=2 × warm). Medians, the first-transfer ratio (`COLD_WARM_FIRST_RATIO`=0.35 as reference) and the exit's SURB target recorded.

### T08-relay-attribution · **gate**

One download of `BYTES` at `hopr_transport::path=debug`; return paths counted per first-hop relay from the `path=[…]` field. Membership: PASS iff every first hop is an `Open` outgoing channel peer of the exit or the exit. Split, gated only with `SUITE_EQUAL_LATENCY`=1, ≥ 2 relays and ≥ `SPLIT_MIN_PATHS`=1000 paths: PASS iff skew = 100·(max−min)/sum ≤ `SPLIT_TOL_PCT`=60; recorded otherwise. WARN with no resolved-path lines.

### T09-impairment-ladder · **gate**

`tc netem` on the host toward the relays' P2P ports, fresh session per cell: `equal-<ms>` for each of `RUNGS`="0 25 50" ("0 25"), `gap-<ms>` (relay 1 only), `far-100ms` (`FAR`=100, relay 2 only). Per cell a 3 Mbit/s download stream of `STREAM_S`=120 s (90; 8) first, then 3 (2) transfer reps. PASS iff every equal cell has `reassembly_failed`=0 and `reconnects`=0. Gap and far cells, stream loss and completions recorded. SKIP without root or below `CLUSTER_SIZE`=3.

### T10-forced-reconnect · **gate**

A `DUR`=300 s (150; 130) call; at `T_KILL`=60 s (20) the client's own WireGuard peer is removed on the exit (found by allowed-ips). Arms T (far end keeps streaming) and S (far end pauses while the client is silent), `REPEATS`=3 (1) each. PASS iff the first downstream packet after the removal arrives within `RECOVER_MAX`=90 s and `DecapStalled`=0 in every repeat; a repeat whose key cannot be read or whose removal fails FAILs.

### T11-capability-matrix · **gate**

Cells over `[connection.wg] capabilities`: `segmentation+no_delay` (shaper expected), `segmentation` (expected), `+no_rate_control` (not expected); per cell a client restart, one `CALL_S`=60 s (30; 8) call and `POLL_N`=10 (4) polls of the exit's per-session `hopr_surb_balancer_*` series. PASS iff `decap_error`=0 and the series count rose exactly when a shaper is expected.

### T12-balancer-sweep · **gate**

Cells `main:<U>` for `UPSTREAMS`="12 16 48 96" Mb/s ("16 96"; "16") plus `ping:10MB`; per cell a client restart, connect time, one cold and one warm download; `PASSES`=2 (1), even passes reversed. PASS iff the worst warm download ≥ 0.3 × the best (best > 0) and the masking cell's cold download on the default ping tier completes within `CAP` (n = 1, whatever the raised tier did). The masking cell's sensitivity is unproven; do not read its PASS as proof the ramp is fixed.

### T13-mtu-sweep · **gate**

Per `MTUS`="1420 1280 940" ("1420 940" very-fast), forced on the interface after connect: `REPS` (2) transfers and a 3 Mbit/s upload stream of `STREAM_S`=120 s (10 very-fast). PASS iff `reconnects`=0 at every MTU and the MTU 940 download median ≥ `MTU940_DOWN_MIN_MBIT`=6. Formerly an XFAIL tagged `FIXED_BY`=hoprnet#8392; plain since 2026-09-20.

### T14-novpn-baseline · *diagnostic*

`REPS` transfers of `BYTES` straight to the target, tunnel down, plus direct RTT. PASS iff all complete; a miss is WARN.

### T15-warmup-knee · *diagnostic*

One download per idle in `DELAYS`="0 5 15 30 60" s ("0 5 20"; "0 5") after a fresh connect. Recorded: the knee (first delay reaching `KNEE_FRAC`=0.8 × the best rate), flagged beyond `KNEE_MAX_S`=30.

### T16-metric-sampling · *diagnostic*

1 Hz node, container and client-balancer samples through one 2×`BYTES` download and one `BYTES` upload. Recorded: exit balancer target and estimate maxima, per-node drop and reject deltas, packet-rate maxima, CPU, balancer-vs-throughput correlation and slope, health-check session count.

### T17-latency-matrix · *diagnostic*

`N`=10 hoprd pings per ordered node pair plus a peer survey; `STDEV_MAX`=25 declared, not asserted. With `SUITE_LATENCY_MAP`="idx=ms …" it gates: node0→idx median within `LATENCY_TOL_MS`=15 of ms + the node0→node1 median, or ≥ ms.

### T18-capacity-ceiling · *diagnostic*

Download streams of `STEP_S`=60 s (30; 12) per rung of `LADDER`="1 2 4 8 12 16" Mbit/s ("2 4 8 12"; "4 8"). Recorded: the knee (last rung with loss < 5 %), CPU per node, watchdog reconnects per rung.

### T19-background-load · *diagnostic*

Client 1 downloads `BYTES` with client 2 idle, then with client 2 fetching `TRICKLES`="10 100" kB ("100" very-fast) every 2 s. WARN when the control is incomplete or a trickle arm is incomplete or below `STEP_FRAC`=0.5 × idle; ratios recorded. SKIP without `CLIENT2`.

### T20-fault-injection · *diagnostic*

During a call: `tc netem` loss over `LOSSES`="1 5 20" % ("5" very-fast) toward relay `RELAY`=1, `STEP_S`=60 s (30; 12) per step, then SIGSTOP for `STEP_S`, then restore. WARN when a rung was not applied; otherwise PASS iff the client is still connected at the end (a miss is WARN). SKIP without root, without a relay pid, or when a qdisc cannot be installed.

### T21-passive-observer · *diagnostic*

Client 2 never connects and polls destination health every 15 s for `DUR`=300 s (60; 20). PASS iff the two clients disagree on the Ready count in ≤ 1/5 of samples. SKIP without `CLIENT2`.

### T22-concurrent-clients · **gate**

Rungs of `LADDER`="1 2 4" clients downloading `BYTES` at once, each warmed with `WARMUP_BYTES`=500000 first (`WARMUP`=1), `CAP` (45 very-fast). Per rung FAIL if any client fails to connect or complete, with the per-client diagnosis. Across the ladder PASS iff no higher rung's aggregate falls more than `TOL_PCT`=25 % below any lower rung's and the top rung's aggregate ≥ `AGG_MIN_MBIT`=8. Fairness recorded. SKIP below 2 clients.

### T23-sustained-soak · **gate**

A `CALL_RATE`=1.5 Mbit/s call for `DUR`=3600 s (600; 60) plus a transfer pair every `INTERVAL`=300 s (30 very-fast); RSS and log growth sampled. FAIL UNMEASURED under `SAMPLE_MIN_PCT`=80 % of expected call packets; otherwise PASS iff `reconnects`=0, final RSS < 2 × initial + 200000 kB, log growth < `LOG_MB_MIN_MAX`=200 MB/min, call loss < `CALL_LOSS_MAX`=5 % (a missing report is 100 %). Deadman covered.

### T24-sustained-upload · **gate**

Upload-only stream at `RATE`=3 Mbit/s for `DUR`=900 s (240; 25) at each of `MTUS`="default 940". PASS iff per MTU loss < 5 %, `reconnects`=0 and the client's undecodable counter grew by < 50; a missing server report is an UNMEASURED FAIL. Deadman covered.

## The runbook

| Entry | Does | Runs when |
| --- | --- | --- |
| T25-knob-ab | cluster restarted with one env var (`KNOB`="HOPR_INTERNAL_IN_PACKET_PIPELINE_CONCURRENCY=nproc×8", or `CLIENT_KNOB` on the client), `REPS` (2) transfers both ways; recorded | `just test t25` |
| T26-version-matrix | one stack per cell of `just matrix cells.txt`, suite per cell; records the cell | `just matrix` |
| T27-role-split | exit version vs relay version | always SKIP: one hoprd binary per cluster (open extension 1) |
| T28-transport-ab | HTTP/1.1 vs HTTP/3; documented negative | SKIP: h3 arm not implemented, needs `TARGET_H3_PORT` |
| T29-destination-sweep | connect, ping, one download per destination, `CYCLES`=3 (1); PASS iff every connect succeeds | production |
| T30-hopcount-ab | `REPS` (2) transfers at 1 hop and 0 hops; PASS iff 0-hop download median ≥ 1-hop | needs `HOPS0_ALSO=1`, `--allow-insecure` |
| T31-frame-forensics | inbound read-length histogram and slab analysis on a cold start; PASS iff no packed slab | needs the instrumented client (extension 3) |
| T32-congestion-control | cubic vs bbr+fq in the client namespace, `PAIRS`=6 (3) ABBA; upload treated, download control; recorded | needs `tcp_bbr` on the host |

## Open extensions

1. **Per-role node versions and env in the localcluster.** Every node comes from one hoprd binary with one `CLUSTER_ENV`; T27-role-split cannot run and T25-knob-ab/T26-version-matrix cannot vary one role. The single most informative missing test.
2. **Client telemetry in the metrics stack.** The suite reads `gnosis_vpn-ctl -o json telemetry` with `docker exec` where it needs a client counter; scraping it at 1 Hz next to the node metrics would let T16-metric-sampling correlate it.
3. **A client build with inbound-read instrumentation** for T31-frame-forensics: a flag or patch that logs the length and leading bytes of every inbound datagram.

Provided by testenv already: the in-cluster target (`just target-start`, `docker/target/`), several funded client containers (`CLIENT_COUNT`, `EXTRA_IDENTITIES`), run metadata and JSONL output, size-capped container logs, live `tc netem` impairment and `CLUSTER_LATENCY`, 0-hop destinations (`HOPS0_ALSO`), a second passive client (`just client2-start`).
