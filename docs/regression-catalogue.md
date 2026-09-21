# Regression test catalogue for `gnosis_vpn-testenv`

Every measurement technique used in the Gnosis VPN throughput and reliability investigations of 2026-09-01..16 (fixed-payload transfer matrices, real-time UDP calls, a controlled exit/relay/client triad, return-relay topology and latency ladders, the sustained-upload SURB overflow, reconnect and endurance runs, relay-health surveys), distilled into 15 gates, 9 diagnostics and 8 runbook entries that run against a `gnosis_vpn-testenv` stack instead of the fleet they were first run on. Each test names the finding it would have caught, so the catalogue doubles as the evidence for why it exists.

The investigation found two real regressions (a client SURB-balancer ramp and a relay decode-concurrency default; reported separately). Both were found by hand. Every test below exists because some ad-hoc probe caught something a scripted suite should have caught first, or because a wrong conclusion was drawn that a scripted suite would have prevented.

## How this maps onto testenv

The suite lives in `scripts/suite/`: one pytest module `tests/test_tNN_<name>.py` per catalogue entry, the library `suitelib/`, the pytest glue `conftest.py`, the version-matrix driver `matrix.py`, the probes in `probes/` (`relprobe.py` for bidirectional echo calls, `streamprobe.py` for one-way paced streams, `callprobe.py` for the forced-reconnect call) and `node-sampler.py` for node metrics. `just suite` runs the whole thing, `just test tNN` runs one entry. Against a production network (a client container pointed at a real exit, a target host running `docker/target`'s services, no localcluster): `just suite --client NAME --dest ID --target HOST --no-cluster`; the cluster-only checks skip and say so. Everything talks to the live stack through `docker exec gnosis_vpn-client gnosis_vpn-ctl …`, the localcluster's `status` JSON and the nodes' REST and `/metrics` endpoints. Traffic goes to the in-cluster target container, which serves the sized download and upload endpoint on `:8899`, a UDP echo on `:8901`, a paced stream server on `:8902` and the two-ended call server on `:8903`.

Conventions, shared with the older tooling in `scripts/`:

- A module docstring that says what the test asserts; every subprocess call has a timeout.
- Every knob is an environment variable with a default, declared in the module's `KNOBS` dict; `q(normal, fast)` gives the `--fast` value. A per-test knob is `T<NN>_<VAR>`, from the environment or `--knob`.
- Each module declares its kind as `KIND = "gate" | "diagnostic" | "runbook"` and reports through the `checks` fixture: `passed`/`failed`/`warn`/`skip` (PASS, FAIL, WARN, SKIP), `record` (RECORDED) and `xfail`. A FAIL from a non-gate is downgraded to WARN, so only a gate can redden a run.
- Every measurement is also a JSON line in `rows.jsonl`, and every verdict a line in `verdicts.jsonl`, so two runs diff cleanly.
- The library has offline unit tests in `scripts/suite/selftest/` (`just suite-selftest`).

Stack features the suite needs and testenv provides: the in-cluster target (`just target-start`), several client containers on pre-funded identities (`CLIENT_COUNT`, `EXTRA_IDENTITIES`, `just clients-start`), size-capped container logs (`LOG_MAX_SIZE`, `LOG_MAX_FILE`), a liveness-ping alias on the server (`SERVER_PING_ALIAS`), 0-hop destinations (`HOPS0_ALSO=1` with `CLIENT_EXTRA_ARGS=--allow-insecure`) and a latency flag passed through to the localcluster (`CLUSTER_LATENCY`). What is still missing is listed under [Required extensions](#required-extensions), and the entries that depend on it say so.

**What the suite discriminates today.** T10-forced-reconnect and T13-mtu-sweep separate versions (one through recovery time, one through a mechanism), T07-cold-start and T12-balancer-sweep pinpoint the SURB ramp, the error counters of T04-fixed-throughput, the membership assertion of T08-relay-attribution and the capability check of T11-capability-matrix are absolute, T06-realtime-udp asserts the shape of a return-path defect, and T01-topology-preconditions refuses to measure through a broken precondition. Two gates fail on the current stack for reasons their entries document: T05-loaded-latency, because six parallel downloads in one session aggregate less than three, and T09-impairment-ladder, because its concurrent CBR stream overloads the download path on every rung. Read them as findings about the stack, not as broken tests, until the mechanism is fixed or the test is reshaped.

## The three kinds

Every entry below is one of three kinds, and they are scored differently. Mixing them was the original mistake in this catalogue: it scored characterizations and fleet techniques as if they were regression gates, which produced red on stacks that had nothing wrong with them.

| Kind | Meaning | Scoring |
| --- | --- | --- |
| **gate** | a regression gate: it must be able to pass on a healthy stack | PASS / FAIL; only a gate can fail a run |
| **diagnostic** | a characterization, control or covariate: measured every run, never scored | RECORDED |
| **runbook** | fleet, investigation or tooling: kept in the repo, in no run | run explicitly with `just test tNN` |

**Thresholds are absolute and named.** Host-dependent numbers were scored as deltas against the previous run until 2026-09-20, inside a band T03-repeatability-baseline measured on the stack. That design ratcheted (a regression that lasted two runs became the reference), mixed run modes (a `--very-fast` 2 MB transfer was judged against the full run's 10 MB one), and on the 2026-09-17 old-version stack it measured a band of ±279 % and let a delivery collapse from 99.7 % to 17.3 % score PASS. Every gate now holds a named absolute floor or ceiling with a default calibrated on the reference stack (hoprd 4.1.3, client 0.96.3, server 0.7.0, one 8-vCPU DigitalOcean host, 2026-09-20). The [threshold table](#thresholds) lists each value, what it was calibrated on and the headroom. T03-repeatability-baseline still runs third and records how far this stack's own numbers wander, so the headroom can be judged; a stack it flags UNSTABLE makes every threshold verdict weak evidence.

**A measurement needs a sample.** A probe whose session is broken sends a handful of packets and still reports a confident percentage from that handful. On the same run T06-realtime-udp's `dl-3Mbit` arm sent 255 of an expected 4688 packets and reported "60.78 % loss", which reads like a result and is not one. Any test that derives a rate or ratio from a probe must check the probe actually ran: T06-realtime-udp computes the expected packet count from rate, size and duration and fails the arm as UNMEASURED below `SAMPLE_MIN_PCT` (80 %), naming both counts.

**Arms that share a session are not comparable.** T06-realtime-udp's six arms used to run inside one connect, so each arm inherited whatever the previous one did to the session. On the old-version stack `echo-1.5Mbit` lost 82.7 % and `echo-3Mbit`, immediately after it on the same session, lost 0.41 %. Ordering, not the stack, produced that difference. Each arm now gets its own session (`PER_ARM_SESSION=1`).

**An expected-failure bound must come from a clean measurement.** T06-realtime-udp briefly carried an XFAIL asserting the bidirectional arm above 1.5 Mbit/s must lose more than 50 %, calibrated on a 65.6 % reading that was later found to be contaminated by the SURB ramp and by arm ordering; the same arm then measured 31 % and 0.41 % on other stacks. The XFAIL was removed rather than re-tuned. An XFAIL silences an arm, so a bound that cannot be justified is worse than a plain gate. T13-mtu-sweep keeps its XFAIL because its mechanism is reproducible and tied to a named fix.

**Gate dependencies.** Some gates cascade: a stack that fails **T07-cold-start** (cold-start ramp) will also fail **T09-impairment-ladder**'s equal-latency rungs and **T04-fixed-throughput**'s completion assertion, because every fresh session in those cells reconnects for the same reason. Fix the ramp first and re-run; do not read three failures as three defects. Verified 2026-09-17 on the fix-surb-ramp client without `GNOSISVPN_SURB_RAMP_SECS=0`: T07-cold-start, T04-fixed-throughput and T09-impairment-ladder's equal rungs all failed on the one ramp, while the gap rungs and set cells (diagnostics) recorded clean.

**Expected failures carry a fix tag.** A known defect with no fix in the tested stack is an `XFAIL` naming the fix it waits on. When the fix lands the test flips to a green gate deliberately, and a surprise pass beforehand is reported as `XPASS` rather than swallowed: an expected-fail that quietly starts passing is the same silence that hid the ramp bug. T13-mtu-sweep carried one for `hoprnet#8392` until 2026-09-20, when the mechanism was gone from the tested stack and the gate went plain; no entry carries an XFAIL now.

## Common harness requirements

These apply to every test that connects the tunnel, and each one is a lesson paid for in lost time.

| Requirement | Why |
| --- | --- |
| **Deadman disconnect** before every connect | The client's kill switch makes the host unreachable while connected. `Client.connect()` in `suitelib/client.py` arms a detached `sleep DEADMAN && docker exec … disconnect` (default 900 s) and `disconnect()` disarms it; a test whose session must last longer calls `client.deadman_cover(DUR)` first. Two bugs in this one helper cost a week of wrong findings. The disarm used to kill the sleep before the bash, and the bash then fell through `sleep; disconnect`, so every connect made in a subshell disconnected its own client about 30 s later: fullrun5's T22-concurrent-clients read zero bytes on five of seven clients with a healthy tunnel ping. And T23-sustained-soak and T24-sustained-upload ran 3600 s and 900 s sessions under the 900 s default, so the deadman disconnected the client at +15 min, the soak counted the rest as loss (24 % delivered) and still passed, and the upload never got its server report. The disarm now kills the bash first, the deadman uses `&&`, and long sessions raise the deadman. On real hosts the timer must be stopped as well as the service: a stopped service with an armed timer produced hours of phantom "tunnel lost" events. |
| **Detached execution** | The controlling shell may lose the host. Run the body detached and read results back afterwards. |
| **Fixed payload, capped duration** | Report bytes moved *and* elapsed *and* whether the transfer completed. A throughput number alone silently conflates "slow" with "truncated at the cap". |
| **Client-log error counters** per run | `frame discarded`, `failed to reassemble`, `decapsulate error`, `tunnel ping exceeded` (reconnects). These separated "slow" from "broken" in every cell and are the primary signal in T04-fixed-throughput. Add the client's own metrics dump (`gnosis_vpn-ctl -o json telemetry`, 45 Prometheus series): `hopr_packet_rejected_count{reason="undecodable"}` is the **only** trace of a reply the client could not open (the log line is trace-level), and it is the signature of the SURB-overflow defect in T24-sustained-upload. Also count `failed to adjust surb balancer`: the client's SURB-target push failing on a live session; 0.96.1 logged it for whole sessions while the exit never received a target update. |
| **One variable per cell** | Every conclusion that later had to be retracted came from a comparison where two things moved at once. |
| **Never load-test through a shared-egress CDN** | The 2026-09-08 concurrency run used a CDN speedtest endpoint; it answered `HTTP 429, Retry-After 3071 s` on the **exit's** egress IP, which rate-limited every real user of that exit for 51 minutes and invalidated the run's collapse at 8 users. This is why the in-cluster target (extension 2) is mandatory rather than a convenience, and why a test must fail loudly on a non-2xx status instead of recording the byte count. |
| **Identity migration invalidates a node until it is re-onboarded** | A client identity copied to a new host read **0.01 Mbit/s** (a stale on-chain address, not hardware) and returned to 4.9 / 7.4 with every transfer complete once re-onboarded. Assert onboarding state before attributing any number to a host, and never compare a freshly moved identity against a settled one. |
| **One identity, one running node** | Node identities are portable by copying `env`, `api-token` and `data/.hopr-id`, which makes accidental double-running easy; the retired containers on our fleet are pinned `--restart=no` for exactly this reason. A test that starts nodes must assert no other container holds the same key. |
| **Channel-set changes have a floor of minutes** | Closing is by counterparty (`DELETE …/channels/<0xpeer>?direction=outgoing`; the channel-id form returns 400), closure sits in `PendingToClose` for 300 s, and the strategy will not reopen to that peer until it clears. Opening blocks on chain confirmation. Any test that varies the relay set (T09-impairment-ladder, T20-fault-injection) must budget this, and the node's allowlist takes 20-element **byte arrays** where the client TOML takes hex. |
| **Debug logging is itself load, and must be identical across a comparison** | `hopr_transport::path=debug` on the client writes ~50 MB/min while connected; exit-side session debug writes ≈4 MB/s and filled a disk before the log cap went in. Scope it to the cell and cap container logs, and keep the level *the same in every cell of one comparison*. Our own final matrix ran the exit at session-debug in all cells: constant, so not a confound, but it means the absolute exit numbers carry that overhead. |
| **No host may hold two roles on the path** | Our own matrix violated this: jura-1 was simultaneously the traffic target **and** one of the exit's two return relays, and the symptom was specific: jura-1 TCP downloads truncating after rep 1 at 0.4–3 Mbit/s while downloads to a third-party target and all uploads stayed fine. The same confound sat unresolved on another exit that was also a transit relay. Declare each host's roles in the run metadata and fail the precondition if any host appears twice. |
| **Assert the path is actually the tunnel** | Two independent checks, both cheap. (a) Fetch the egress IP through the tunnel and require it to equal the exit's; anything else fails the run rather than silently measuring the clear net. (b) Apply a throughput ceiling: relay IPs sit in the kill-switch allowlist, so an unbound socket reaches the target directly, and when the interface disappears a `SO_BINDTODEVICE` shim's `setsockopt` fails **silently**: a leak showed up as 100–170 Mbit/s. Treat any tunnelled transfer above ~30 Mbit/s as a leak, not a result. |
| **Every other configured destination is hidden load** | While connected, the client health-checks every *other* `[destinations.*]` entry through the mixnet about every 15 s, each one a session that mints SURBs; in-tunnel RTTs of 2–4 s were observed during downloads because of it. The number of configured destinations is therefore a variable in every cell: pin it, record it, and keep it identical across a comparison. |
| **Intra-cell ordering and statistics** | Leave a settle gap between the two directions (15 s over the mixnet; ~3 s on a local link, which has no mixnet tail) so the second direction is not measuring the first one's congestion. Take the RTT burst *unloaded*, before any transfer. Report median, not mean, with the spread: mixnet latency is skewed enough that one outlier drags the average off the typical packet, and keep every reply and every lost sequence number rather than a summary. |
| **Analyse big logs in place** | A full-debug client log runs to hundreds of MB per session and idle debug has reached ~15 GB/h; one campaign filled 16 disks and lost its first log collection. Run the reducer on the host and fetch only its output, in a gap between sessions. Never copy the log. |
| **Host tuning breaks comparability with your own history** | Enabling BBR + fq system-wide inflates the monitor's upload figures against its own pre-tuning series, and the client package's postinstall drops a sysctl file that does this silently. Record the host's congestion control and qdisc in the run metadata (T02-build-provenance) and never diff across a tuning change. |
| **Record component provenance** | Version string *and* the actual build identity of client, servers and each cluster node, in the run's metadata (see T02-build-provenance). |
| **Worker keepalive and liveness** | The client worker started with `start-client <duration>` expires silently; every connect after that fails with no error in `status`. Verify the worker is running and renew the keepalive before every cell, and record the keepalive deadline in the run metadata. One whole night of ladder repeats was lost to an expired 12 h keepalive. `systemctl restart gnosisvpn.service` returning is not readiness: the control socket answers `rc=69 … service not running` for a second or two, so retry the command until it is accepted. Anchor the worker check (`pgrep -f '^/usr/bin/gnosis_vpn-worker'`); an unanchored `pgrep -f` matches its own shell and is always true. |
| **Config changes restart the worker** | Any write to the client's `config.toml` reloads the worker, which drops the session. Treat every config change as a fresh session (T07-cold-start applies) and never change config inside a cell. A watchdog-tolerance change applied itself mid-run this way. |
| **Probes bound to the tunnel interface must survive reconnects** | A UDP probe bound with `SO_BINDTODEVICE` goes blind after a client reconnect and reports 100 % loss that is not there. Detect and count rebinds (the `rebinds` field in `call.summary`), and re-bind on reconnect. |
| **Leftover impairment** | `tc netem` left on a relay from a previous cell, or an old deadman timer, silently poisons the next run. Assert "no qdisc other than the default, no armed timers" as a precondition (T01-topology-preconditions) and remove impairment in a `trap` on exit. |
| **Fleet preflight on real hosts** | Before a campaign: every host answers SSH with an *unchanged* host key and the stored credential, runs the expected containers at the expected versions, has disk headroom, and shows no leftover qdisc, timer or foreign hoprd. Four Contabo probes were reprovisioned during a decommission without anyone noticing; their host keys and passwords had changed and they were still listed as available. |
| **Out-of-band recovery for real hosts** | On real hosts a connected client is only reachable through something outside the kill switch (`bin/vpn-guard`, a cloud console). testenv containers do not need this, but any test that later runs on hosts does. |
| **In-tunnel probes: bind to the tunnel interface, aim beyond the exit** | An unbound `ping` issued before the tunnel exists leaves over the physical interface, succeeds in ~3 ms and reports a TTFB of nothing. Bound (`ping -I wg0_gnosisvpn`) it fails closed until the tunnel is real. Never target the WireGuard peer (`10.128.0.x`): it answers locally in 0.05 ms without crossing the mixnet. Take transfer totals from curl's `-w` output, never its progress meter (binary units, three significant digits). Capture a response body to a file before matching it: piping `curl` straight into `grep -q` is a SIGPIPE flake under `pipefail`. |
| **Snapshot identity and data dir before every version swap** | A newer hoprd rewrites `.hopr-id` in a format the older one cannot decrypt; a downgrade cell then crash-loops with `password is not sufficient to decrypt` (4.0.3 after any 4.1.x or `08d777e4` run). Back the data dir up before each cell, restore the identity on downgrade, and assert the node comes up with the same peer id and chain address afterwards, or the cell measured a different node. |
| **Report n, sign test and spread, never a median alone** | Three findings were published and retracted in these campaigns. A correlation near ±1.00 here is an identity (SURB legs/s vs downloaded bytes; worker CPU vs throughput), not a cause. Four A/B pairs agreeing is p = 0.125 and happens constantly: a −8 % download effect at n = 4 became +4 % at n = 13. Every comparison row carries n, the per-pair sign count and the range, and any effect on a quantity the cell could not have moved (a client congestion-control change moving the *download*) marks the pair as drift. |

---

# Classification

| Kind | Tests |
| --- | --- |
| **gate** | T01-topology-preconditions (incl. the client channel pin), T02-build-provenance, T04-fixed-throughput (error counters and throughput floors), T05-loaded-latency (p95 ceilings; must run early), T06-realtime-udp (all four arms gated), T07-cold-start, T08-relay-attribution (membership; split gated only at equal latency), T09-impairment-ladder (equal-latency rungs), T10-forced-reconnect, T11-capability-matrix, T12-balancer-sweep (incl. the masking cell), T13-mtu-sweep, T22-concurrent-clients, T23-sustained-soak, T24-sustained-upload |
| **diagnostic** | T03-repeatability-baseline (records the stack's repeatability), T14-novpn-baseline, T15-warmup-knee, T16-metric-sampling (incl. the health-check covariate), T17-latency-matrix (gate only when a cell injects an impairment map), T18-capacity-ceiling (incl. the watchdog rung), T19-background-load, T20-fault-injection, T21-passive-observer |
| **runbook** | T25-knob-ab, T26-version-matrix, T27-role-split, T28-transport-ab, T29-destination-sweep, T30-hopcount-ab, T31-frame-forensics, T32-congestion-control |

Promoted in this revision:

| Was | Now | Why |
| --- | --- | --- |
| T06-realtime-udp diagnostic | **T06-realtime-udp gate** | recording 65 % loss on the round-trip arm and moving on made the suite's central defect invisible. Every arm is now a binary verdict rather than a number nobody reads (an XFAIL on the round-trip arm was tried and removed, see above) |
| T05-loaded-latency diagnostic | **T05-loaded-latency gate** | a 3x loaded-RTT regression produced a WARN that nobody acts on. It was demoted because it ran last and could not be told apart from host load, a gap in the method rather than a property of the metric. Fixed by ordering it early, then gating it |
| T22-concurrent-clients runbook | **T22-concurrent-clients gate** | testenv now runs several client containers, so the ladder runs locally. Its aggregate rule is one-sided (no higher rung may collapse below a lower one) after the symmetric rule failed a healthy ladder whose aggregate rose with concurrency |

Numbering note: test IDs follow the run's execution order: T01–T24 are the run, in the order pytest collects them (file order), and T25–T32 are the runbook (in no run). The number reflects position, not kind: a diagnostic such as T03-repeatability-baseline is numbered third because it must write the scoring band before any gate, The Classification table above is authoritative for kind.

Merged or demoted in this revision, with the reason:

| Was | Now | Why |
| --- | --- | --- |
| T15-warmup-knee gate | T15-warmup-knee diagnostic (knee) | a fresh session always warms up, so "flat across the sweep" is not a healthy-stack property; it failed even on ramp-off stacks that pass T07-cold-start. T07-cold-start is the gate |
| the former config-default-vs-tuned test | a cell of T12-balancer-sweep + a T01-topology-preconditions precondition | in testenv the "tuned deployment config" does not exist; the former config-default-vs-tuned test reduced to one T12-balancer-sweep cell plus the effective-config diff |
| the former synthetic latency ladder | folded into T09-impairment-ladder | one impairment test parametrised on both axes; the mixed-RTT collapse only appears when the channel set and the latency vary together |
| the former watchdog-under-saturation test | the top rung of T18-capacity-ceiling | watchdog behaviour above the knee is a row of the capacity ladder |
| the former reconnect-cycle endurance test | the RSS assertion in T23-sustained-soak | forty-five cycles to re-confirm a negative is not a regression test; the one live covariate is a soak assertion |
| the former channel-strategy test (a)(b) | dropped; (c) into T01-topology-preconditions | a selector needs a peer population a 3-node cluster lacks, and (b) tested our own tool; the client pin is a precondition |
| the former health-check-load test | a covariate in T16-metric-sampling | flaky by construction and the effect is tiny at three destinations |
| T14-novpn-baseline, T17-latency-matrix, T21-passive-observer | diagnostics | controls, not discriminating gates on one host, but deleting a control loses the "is the rig working" check |
| T28-transport-ab, T32-congestion-control, T31-frame-forensics, T30-hopcount-ab, T29-destination-sweep, T27-role-split, T25-knob-ab, T26-version-matrix | runbook | documented negatives, fleet-only levers, investigation technique, or tooling that should print RECORDED, not PASS |

# Test catalogue

Entries follow in test-ID order, which is the run's execution order: T01–T24 are the run in the order pytest executes it, T25–T32 the runbook. Each heading carries the entry's kind:

- **gate**: must be able to pass on a healthy stack; the only kind that can fail a run.
- *diagnostic*: measured and recorded every run, never scored: it characterizes the stack, verifies the rig, or carries a covariate; a number here is evidence, not a verdict.
- *runbook*: fleet, investigation and tooling entries; kept in the repo with their scripts but in no run, executed explicitly with `just test tNN`. Each says why it does not belong in a local regression run.

**Common parameters** (from `suitelib/config.py`; a `--fast` or `--very-fast` run substitutes the value in parentheses): `BYTES`=10000000 (`--fast` 5000000, `--very-fast` 2000000), `CAP`=90 s (`--fast` 60, `--very-fast` 30), `REPS`=3 (`--fast` 2, `--very-fast` 1), `SURB_RAMP_WAIT`=25 s (the floor on every post-connect idle unless the test connects with `ramp_wait_opt_out=True`), `CONNECT_TIMEOUT`=240 s, `TEST_TIMEOUT`=7200 s (every test is failed past it unless its module computes its own `TIMEOUT(knobs)` from its durations; every subprocess call has a timeout of its own), `DEADMAN`=900 s (a test whose session must last longer calls `client.deadman_cover(SECONDS)`, which raises it to SECONDS + `SURB_RAMP_WAIT` + 120 s; T23-sustained-soak and T24-sustained-upload do), `DEST`=node-0, `CLUSTER_SIZE`=3. **`--fast` rule:** a quick value never drops a sustained-flow arm below 90 s, so that anything on the timescale of the client's liveness watchdog (3 pings × 10 s interval + 15 s timeout ≈ 85 s) is still inside every arm; `--very-fast` (15 s arms) is the mode that trades that visibility for time. Every `Pass criteria` below states what the script enforces; anything the original design asked for but the script does not assert is marked "recorded, not asserted".

<a id="thresholds"></a>
**Thresholds.** Every number a gate holds a measurement against, with its default, where it is used, what the reference stack measured, and why the value sits where it does. Nothing refers to a previous run. The values are calibrated for full-length runs; a `--very-fast` run moves 2 MB per transfer, which is mostly ramp, and reads 20-40 % lower (T13-mtu-sweep's MTU 940 rung measured 7.0 against a floor of 6), so its threshold verdicts are smoke-level.

| Knob | Default | Used by | Reference stack measured | Why this value |
| --- | --- | --- | --- | --- |
| `DOWN_MIN_MBIT` | 7 Mbit/s | T04-fixed-throughput download median | 10.9 (stdev 0.45 over 10 warm sessions), 11.05 in fullrun6 | the 2026-09 relay regression halved throughput, so 7 catches a halving and clears the host's ±12 % noise |
| `UP_MIN_MBIT` | 7 Mbit/s | T04-fixed-throughput upload median | 13.0 (stdev 0.69), 12.95 in fullrun6 | a halving lands at 6.5; 7 is 54 % of the median |
| `DOWN_P95_MAX_MS` | 1500 ms | T05-loaded-latency loaded RTT p95 during a saturating download | 359, 537 and 1076 ms over three full runs | the fleet's bufferbloat finding was 2-4 s; 1500 leaves 40 % over the worst healthy reading |
| `UP_P95_MAX_MS` | 2500 ms | T05-loaded-latency loaded RTT p95 during a saturating upload | 1124, 1153 and 1746 ms | a parallel upload on the fleet hit 8.9 s; 2500 leaves 40 % over the worst healthy reading |
| `LOSS_MAX` | 5 % | T06-realtime-udp, every arm | 0.02-1.7 % | a call above 5 % loss is audibly broken; the fleet's defect read 54-96 % |
| `STALL_MAX` | 5 s | T06-realtime-udp, every arm | 0-1.04 s worst stall | a 5 s gap is a dropped call, not jitter |
| `SAMPLE_MIN_PCT` | 80 % | T06-realtime-udp probe sample guard | 100 % | a probe that sent less did not run; its loss figure is meaningless |
| `CALL_LOSS_MAX` | 5 % | T23-sustained-soak call | 0.09 % (very-fast), 0.0 % on a clean 20 min soak | the same probe and rate as T06-realtime-udp's echo arm, so the same bound |
| `RECOVER_MAX` | 90 s | T10-forced-reconnect first downstream packet after the peer removal | 72-83 s | the client notices a removed peer only through three liveness-ping cycles, about 75 s; the gate sees a regression of more than about 10 s |
| `MTU940_DOWN_MIN_MBIT` | 6 Mbit/s | T13-mtu-sweep download median at MTU 940 | 12.3 (full), 8.6 (very-fast, 2 MB) | under T04-fixed-throughput's floor because a 940 B MTU carries about a third more packets per byte |
| `AGG_MIN_MBIT` | 8 Mbit/s | T22-concurrent-clients aggregate at the top rung | 16.1 at n=4 | below one client's rate, so it fires only on a collapse; completion and the one-sided drop rule do the finer work |
| `TOL_PCT` | 25 % | T22-concurrent-clients drop from a lower rung to a higher one | aggregate rose 10.6 → 13.7 → 16.1 | outside the host's ±12-18 % repeatability, inside a real collapse |
| `COLD_DECAP_MULT`, `COLD_DECAP_FLOOR` | 2, 5 errors | T07-cold-start cold-arm decapsulation errors against the warm arm's | 0 in both arms on the reference stack | a cold session may see a few, not a burst; medians are recorded, not gated |
| `SPLIT_TOL_PCT`, `SPLIT_MIN_PATHS` | 60 %, 1000 paths | T08-relay-attribution return-path split at equal latency | 8 to 72 % skew on the reference stack | 60 fails an 80/20 split and beyond (85/15 is skew 70 and fails); this gate is expected to fail on some healthy runs and is kept as a signal by decision; below the path floor the split is recorded, not gated |
| `UNSTABLE_PCT` | 50 % | T03-repeatability-baseline flag | 12.1 % | above it the stack cannot repeat its own numbers and every threshold verdict is weak evidence |
| `LOG_MB_MIN_MAX` | 200 MB/min | T23-sustained-soak client log growth | 63-70 MB/min at path-planner debug | the incident was 1.6 GB/min; 200 is three times the normal rate |

## T01-topology-preconditions: Topology and funding preconditions  ·  **gate**


**Question.** Is the mixnet in the state the test assumes, *before* the test runs?

**Method.** Read the localcluster's status and every node's channel set, open a 1-hop UDP session from the exit node through each relay, wait for the client worker, for `DEST` to be Ready and for the client's own channel, read the server's `wggvpn` addresses against both liveness-ping targets, and check the host for leftover `netem` qdiscs and timers. Record the effective client config against the shipped defaults. Abort the suite rather than measure through a broken precondition. Channel balance and unexpected peers on the exit are not checked.

**Parameters.** `FWD_TIMEOUT`=20 s (the forwarding probe must establish within it), `READY_TIMEOUT`=300 s, `CLIENT_CHANNEL_TIMEOUT`=240 s, `CLUSTER_SIZE`=3, `PERIODIC_PING_TARGET`=10.128.0.1.

**Pass criteria.** FAIL, and the run aborts, unless all of: cluster state is `running`; every node reports `channels_open`; every node has at least `CLUSTER_SIZE`−1 = 2 outgoing channels in `Open`; with `CLUSTER_SIZE` ≥ 3, a 1-hop UDP session from node 0 to every other node establishes within `FWD_TIMEOUT`=20 s (a later success is a FAIL naming the duration; the session is deleted either way); the client worker is online within 120 s; `DEST` (node-0) is Ready within `READY_TIMEOUT`=300 s; the client holds ≥ 1 outgoing channel within `CLIENT_CHANNEL_TIMEOUT`=240 s; both the configured `[connection.ping] address` and the client's hardcoded periodic liveness-ping target `PERIODIC_PING_TARGET`=10.128.0.1 are addresses on the server's `wggvpn` (client ≤ 0.96.3's `tunnel_ping_loop` ignores the configured one, and a missing target makes every session reconnect every ~85 s: two full runs were misread as a load defect on 2026-09-19 because of it); no `netem` qdisc is present on the host. WARN only: another destination not Ready; an armed suite or deadman timer. The effective-config diff against the shipped defaults is recorded, not asserted.


**Further preconditions**, each from a separate incident:

- **Relay forwarding probe, not just ping.** From the exit's API, open a 1-hop session forced through each candidate relay with a 0-hop return (`forwardPath: {Hops: 1}` toward a destination that makes the relay the only choice) and require it to establish within `FWD_TIMEOUT`=20 s. A production relay (jura-1) pinged at 6 ms with a perfect probe rate while every session through it timed out at 70 s; it silently broke half of all 1-hop health checks for three hours. Its metric signature, to assert on: climbing `hopr_egress_ring_buffer_dropped`, `hopr_packet_rejected_count{reason="invalid_ticket"}`, `decode timeout` lines, and 60k/30 min `balance of channel … too low` lines.
- **Route health per hop count.** Assert every destination reports `Ready` at *its* hop count. The 0-hop route to the same exit was `Ready` throughout the incident above; only the 1-hop route failed, and the distinction is the diagnosis.
- **Identity lease.** Assert no other node on the network announces or is connected under any identity used in the run (Blokli `accounts` by chain key, plus the peer table). Three identities in the fleet were moved between hosts; the old containers are pinned `--restart=no`, and a double-run would have been invisible to every throughput number.
- **Announced address matches the host**. For every controlled node, the multiaddress it announced (Blokli, the peers' `network/connected` view, or the client's `external address` log line) must carry the IP it currently runs on. A *client* identity moved between hosts does **not** re-announce: peers keep dialling the old IP, the return path starves, and throughput collapses to ~0.01 Mbit/s with 11–36 s TTFB while the tunnel reports up. (A hoprd relay identity does re-announce with `--announce --host`.)
- **Funding noise.** Count `channel-lifecycle: funding tx failed` during the run; a burst every ~63 s is a top-up colliding with a pending transaction, and the channel may drain mid-cell.
- **Channel state, including the exit's outgoing set** (see T08-relay-attribution) and the client's channel balance against the planned payload's ticket cost.
- **No leftover impairment or timers** (harness table).

**Why it exists.** Two separate incidents. A relay silently rejected tickets for 2.5 hours and invalidated every run in the window. And an unidentified foreign client held a WireGuard peer on our exit for the entire investigation, harmless as it turned out, but only because it was found and quantified rather than assumed.

**Capacity and economics amendments**. Two additions, both of which silently invalidated real runs.

*Slots are a design precondition, not a result.* Read the exit's published session-slot count before designing anything around a client count: most exits published 2 slots while one published 16, so a ladder above two clients had exactly one exit it could run against. Occupancy can be seconds stale, so attempt the connection anyway and re-read slots only to explain a failure.

*Funding is not a one-time check.* A relay can exhaust its channel **mid-campaign**: ours began rejecting every packet from the exit at a known minute ("ticket value is greater than remaining unrealized balance") because its redemptions were timing out at its chain client, leaving the channel drained, and six named benchmark cells afterwards ran with half the return path dead before anyone noticed. Poll the precondition *during* long runs, and know the signature so it is not misread as a code regression: client frame discards **plus** "no surb for pseudonym" on the exit **plus** "failed to validate ticket" on the relay. The inverse also exists: a channel-sizing config that is unfundable for the network's ticket price opens zero channels and every health check then times out with no error line anywhere.

## T02-build-provenance: Build provenance and protocol compatibility  ·  **gate**


**Question.** What is actually running, and can these components talk to each other?

**Method.** For each binary/image in the stack, record the version string, the image digest and OCI revision label, and the wire-protocol identifier compiled in where one exists (client worker and hoprd; the exit server has none). Assert the client and hoprd share one protocol identifier. Store the result as the run's metadata header.

**Parameters.** none.

**Pass criteria.** PASS when the `/hopr/mix/<ver>` protocol id read from the client worker binary and from the hoprd binary are both present and equal; FAIL when both are present and differ, which is the incompatibility the gate exists for; WARN when either cannot be read. The exit server embeds no such id (it drives its hoprd node over REST; its binary holds no `/hopr/` string), so its version is recorded and its id joins the comparison only if a future build carries one. It writes `provenance.json` for the run.


**Why it exists.** A release's lockfile claimed a library version (from a different branch, with a wire-format-breaking change and a bumped protocol identifier) that was **not** in the shipped binary. Trusting it would have produced an entirely fictitious analysis. Reading the identifiers out of the binaries took one command and settled it. Since a HOPR packet's frame size is fixed independently of what is inside it, mismatched versions can misparse rather than fail to connect, which makes this check a genuine correctness guard rather than bookkeeping.

## T03-repeatability-baseline: Repeatability baseline: how many runs is a number?  ·  *diagnostic*


**Question.** For this stack, how many repetitions does a claim of a given effect size need?

**Method.** Repeat one unchanged cell (T04-fixed-throughput, warm) `N` times back to back, with no version, config or topology change, and report median and spread per direction and the minimum detectable effect at the suite's `REPS`. It runs third, before any gate reads a number, because the answer is a property of the stack.

**Parameters.** `N`=10 sessions (`--fast` 5, `--very-fast` 3); each is one connect with a 25 s idle, one download and one upload of `BYTES`.

**Pass criteria.** none. RECORDED: n, medians and stdev per direction, and `mde_pct` = max over download and upload of 2·1.96·stdev/mean/√`REPS`·100, the smallest effect a `REPS`-transfer median can resolve on this stack. Records `UNSTABLE BASELINE` when `mde_pct` > `UNSTABLE_PCT`=50; nothing else consumes the number, it is there to judge the thresholds' headroom.


**Why it exists.** "A single client cannot get a repeatable number on this network" was an explicit conclusion of the 40-run exit cycle, where deviations were as large as the medians, and the same exit varied 2× run to run between two 16-client ladders. Without this number, any comparison is unfalsifiable, and a threshold's headroom cannot be judged. It is also the cheapest defence against the failure mode that cost this investigation the most: reading a real effect into what was drift, and reading drift into what was a real effect.

## T04-fixed-throughput: Fixed-payload throughput matrix (the core regression test)  ·  **gate**


**Question.** For a given stack, does a bulk transfer of known size complete, how fast, and how many frame-level errors does the client record?

**Method.** One session. `REPS` iterations of: download `BYTES` from the target, then upload `BYTES` to it, each capped at `CAP` seconds. Record per transfer: direction, rep, bytes, elapsed, time-to-first-byte, computed Mbit/s, HTTP status, completed-or-truncated. After disconnect, count the four client-log error classes over the session's log slice, and diff the client telemetry counters taken before connect and after disconnect (`undecodable`, SURB produced/consumed). Emit one JSONL row per transfer plus one summary row. Sample bytes transferred **per second** during every transfer and report the longest zero-progress interval next to the mean: a transfer that stalls for 5 s and one that is uniformly slow have the same Mbit/s, and the stall is the user-felt event (the 2026-09-01 per-second timelines and the 2026-09-13 `hist-*.csv` series both exist only because the per-transfer number hid it).

**Parameters.** `WAIT_AFTER_CONNECT`=0 s (floored to `SURB_RAMP_WAIT`=25), `BYTES`, `CAP`, `REPS` (common).

**Pass criteria.** PASS iff download completions = `REPS` and upload completions = `REPS` and the client-log counters over the session read `reassembly_failed` = 0 and `reconnects` = 0, and the download median ≥ `DOWN_MIN_MBIT`=7 Mbit/s and the upload median ≥ `UP_MIN_MBIT`=7 Mbit/s. Frame discards and decapsulation errors are reported, not asserted.


**Why it exists.** This is the harness that produced every cell in the investigation. Its three outputs are non-substitutable: completions caught truncation, Mbit/s caught the 2x relay regression, and the error counters caught the client regression (uploads at 10 Mbit/s while downloads died at 0.37: a mean would have shown "5" and hidden both).

**testenv notes.** Uses the in-cluster traffic target (`just target-start`); a public endpoint would make the numbers depend on the internet path. Control via `docker exec gnosis_vpn-client gnosis_vpn-ctl`; run curl **inside the client container's namespace** (`docker exec`), since that is where the tunnel routes live.

**Byte-chain amendment**. Read the WireGuard counters at **both** ends of the tunnel around each transfer, not just the client's throughput. It separates "the path dropped it" from "the endpoint never sent it", which no single-ended number can: in one case the exit injected 8.88 MB and the client received 7.46 MB (84 % delivered), but both ends were injecting only ~1 Mbit/s against 7–9 available, so the loss was real and yet not the limit. That pair of facts is what ruled out relay packet loss as the cause with data instead of argument.

## T05-loaded-latency: Loaded latency (bufferbloat) and parallel-flow scaling  ·  **gate**


**Question.** How much does the tunnel's latency inflate under a saturating transfer, and does adding parallel flows inside one session raise aggregate throughput, or only latency?

**Method.** One session. Measure in-tunnel RTT idle, then during a saturating download, then during a saturating upload, `PHASE_S` each; report RTT p50 and p95 per phase. Then N in `PARALLEL` concurrent downloads of `BYTES` in the same session, reporting aggregate Mbit/s per N. Uploads are not run in parallel: the parallel-flow question is about the SURB-metered return path that downloads ride, where the fleet's scaling finding was; uploads ride the forward path, which T24-sustained-upload exercises alone.

**Parameters.** `PHASE_S`=30 s per ping phase (`--fast` 15, `--very-fast` 8), `PARALLEL`="1 3 6" (`--very-fast` "1 3"), `DOWN_P95_MAX_MS`=1500, `UP_P95_MAX_MS`=2500.

**Pass criteria.** FAIL if any parallel flow does not complete within `CAP`, naming the rung, the count and the bytes received; otherwise FAIL iff for any successive N in `PARALLEL` the aggregate download rate (bytes actually received × 8 over the rung's wall time) falls below 0.8 × the previous N's aggregate. Until 2026-09-20 the aggregate was N × `BYTES` over wall time, so a flow that hit `CAP` still counted as `BYTES` delivered: three full runs read exactly 5.33 Mbit/s at N=6, which is 6 × 10 MB × 8 / 90 s, the cap and not a throughput. The loaded RTT p95 must stay ≤ `DOWN_P95_MAX_MS`=1500 ms during the download phase and ≤ `UP_P95_MAX_MS`=2500 ms during the upload phase. Ordering is part of the test: it runs immediately after T04-fixed-throughput so the host is not yet loaded by earlier tests.


**Why it exists.** 2026-09-02: N = 1/3/6 downloads through one tunnel gave 5.7 → 5.6 → 3.3 Mbit/s (USA) and 8.2 → 8.2 → 6.8 (NL), flat then congesting, while loaded RTT went 216 → 511 → 722 ms and a parallel upload hit 8.9 s: the HOPR session path is the cap and its queues bloat. The same shape is the 2026-09-15 queue-depth decision (UDP ingress 8 192 → 256 datagrams, kernel drop beyond) and the residual ≈8 s cold-start bubble that sits in hoprd's `2 × target` session egress buffer. No test above measures latency *under load*: T06-realtime-udp measures a paced flow, T04-fixed-throughput a throughput number.

**testenv notes.** Purely local against the in-cluster target, so the loaded RTT is the mixnet's.


## T06-realtime-udp: Real-time UDP behaviour under constant bitrate  ·  **gate**


**Question.** Can the tunnel carry a fixed-rate real-time flow, as opposed to a bulk TCP transfer?

**Method.** Five arms, each on its own session. An idle control (connect and send nothing for `ECHO_DUR`), then a bidirectional echo call (`relprobe.py` against the target's `:8901`) at `ECHO_RATE` for `ECHO_DUR`, then an upload-only and a download-only paced stream (`streamprobe.py` against `:8902`) at `STREAM_RATE` for `STREAM_DUR`, then the download stream once more after `REPS` bulk transfers on the same session. Each probe reports loss, the delay distribution above its minimum, stalls, rebinds and outage time.

**Parameters.** `ECHO_RATE`=1.5 Mbit/s, `ECHO_DUR`=300 s (`--fast` 120, `--very-fast` 15), `STREAM_RATE`=3 Mbit/s, `STREAM_DUR`=120 s (`--fast` 90, `--very-fast` 15), `SIZE`=1200 B, `LOSS_MAX`=5 %, `STALL_MAX`=5 s, `SAMPLE_MIN_PCT`=80, `PER_ARM_SESSION`=1, `AFTER_BULK`=1, `IDLE_ARM`=1. Five arms, each on its own session: an idle control with no traffic for `ECHO_DUR` (black box: the session must simply survive), one bidirectional echo call at `ECHO_RATE` for `ECHO_DUR`, an upload-only and a download-only stream at `STREAM_RATE` for `STREAM_DUR`, and the download stream again on a session that has just carried `REPS` bulk transfers (`--fast` 1). That last arm isolates what T09-impairment-ladder stumbled on: on hoprd 4.1.3 a 3 Mbit/s download stream straight after bulk transfers showed 9-54 reassembly failures and up to 71 % loss in three runs while the fresh-session arm read 0.2 %. Until 2026-09-19 this was six arms (three directions × 0.5/1.5/3 Mbit/s, 300 s each): across two full runs every arm reconnected at ~50 s regardless of rate or direction, so the rate axis discriminated nothing. That reconnect was the client's periodic liveness ping targeting an address the server did not hold (see T01-topology-preconditions), not the load; the three-arm shape stays because it measures more in less time. The long echo arm is the gate; the two streams discriminate the direction.

**Pass criteria.** The idle arm: FAIL when the client log shows any reconnect during `ECHO_DUR` s with no traffic (an idle reconnect means the liveness ping or the session itself is failing, and no loaded arm then measures load), WARN on a tunnel-ping timeout without a reconnect, PASS otherwise. Per loaded arm (the echo call, upload-only, download-only, and download after bulk, each on its own session), checked in this order: FAIL `RECONNECT` when the client log for the arm shows any reconnect (`reconnects` > 0), whatever the loss figure. The verdict names the reconnect count, the probe's rebinds and the outage seconds; FAIL `UNMEASURED` when the probe sent fewer than `SAMPLE_MIN_PCT`=80 % of the expected `RATE`·10⁶/8/`SIZE`·duration packets; FAIL when the probe returns no loss figure; FAIL when loss ≥ `LOSS_MAX`=5 % or any receive gap exceeds 5 s; PASS otherwise. The probes re-bind their socket when the tunnel interface is recreated and keep their local port, so loss means loss on the live path; the outage a reconnect caused is reported as `outage_total_s` beside it, not folded into it. Before 2026-09-19 the probes stayed bound to the removed interface and went blind, which made every arm read loss = (arm length − 50 s) / arm length.


**Method details.** Run each direction **alone** (upload-only, download-only) as well as bidirectionally: the two directions fail differently (uploads via the forward path, downloads via the SURB-metered return path) and a bidirectional run averages them away. Include one rung above the exit's capacity (T18-capacity-ceiling) so the queueing signature (RTT climbing to 2–6 s from minute 0, no loss) is recorded and not mistaken for loss. Use 5-minute cells for sweeps and 30-minute cells for the rates that matter (1.5 / 3 Mbit/s).

**Attribution amendments**. Log every packet on **both** ends (sender sequence and timestamp, receiver arrival) and join the logs, so each lost packet is attributed to the forward or the return leg, and report delay per leg *above the session minimum*, because the two clocks are not synchronised, so absolute one-way delay is meaningless. Run an **underlay ICMP control** at 1 Hz between exit, relay and far end for the whole cell so a loss episode can be cleared of the raw network; **never ping the connected client host from outside**: the kill switch drops it and every episode then reads as an underlay timeout (the 2026-09-14 attribution report carried that false cause on all 64 episodes). Cluster losses into episodes (gaps < 1 s) and attribute each against the taxonomy that was needed in practice: watchdog teardown, exit `no surb`, client opener evictions (replies undecryptable), client / exit frame-reassembly deadline, relay CPU burst, egress ring-buffer drops, mixer backlog, underlay timeout, unattributed; report packets lost per cause. The far end needs an **idle-pause** mode (stop streaming while the client is silent > 2 s, resume on its next packet) as the control arm for T10-forced-reconnect.

**Why it exists.** TCP bulk transfers hide return-path loss behind retransmission; a fixed-rate UDP flow does not. Earlier measurements recorded 54–96 % loss at 1.5 Mbit/s and ~100 % at 3 Mbit/s on a path whose TCP throughput looked acceptable. A suite with only T04-fixed-throughput would call that path healthy.

**testenv notes.** Ship the sender as a small container on the testenv Docker network; run the receiver inside the client container's namespace. Purely local, so the measured delay is the mixnet's.

## T07-cold-start: Cold vs warm session start  ·  **gate**


**Question.** Does load applied immediately after tunnel-up behave differently from load applied after the session has settled?

**Method.** Two otherwise identical T04-fixed-throughput runs: the cold arm measures straight after connect (ramp wait opted out) and the warm arm after `WARM`=25 s. Compare completion counts and error counters, not just throughput.

**Parameters.** `WARM`=25 s idle for the warm arm; the cold arm idles 0 s with `ramp_wait_opt_out=True`; `REPS` transfers per arm (`--fast` 1); `COLD_WARM_FIRST_RATIO`=0.35, `COLD_DECAP_MULT`=2, `COLD_DECAP_FLOOR`=5.

**Pass criteria.** PASS iff cold `reconnects` = 0 and warm `reconnects` = 0 and cold download completions ≥ warm completions and cold-arm decapsulation errors ≤ max(`COLD_DECAP_FLOOR`=5, `COLD_DECAP_MULT`=2 × the warm arm's). The cold and warm medians are recorded, not gated (until 2026-09-21 the knobs were named as if they bounded the medians while the code bounded the decapsulation errors). The cold/warm first-transfer ratio is RECORDED with `COLD_WARM_FIRST_RATIO`=0.35 as a reference, not gated: three clean cold starts measured 0.48, 0.34 and 0.25 and every threshold tried failed a healthy one. The exit's `hopr_session_surb_target_buffer` at cold load start is recorded, not asserted. The first-transfer ratio is deliberately loose: a fresh 0.96.x session measures the SURB ramp, whose first transfer is about half of steady state by design (see T15-warmup-knee), so the real cold-start signal is completion with zero decap and zero reconnect, not the cold arm matching the warm one. A ratio near the expected ~0.5 ramp ratio would fail every healthy cold start.


**Not implemented in the script; kept as the design notes they are**:

- The exit's per-session `max_surbs_per_sec` equals `target / minimum_surb_buffer_duration` for the *main* target, and the promotion arrives within a few seconds of tunnel-up, not minutes (the 0.96.1 ramp: +147 every 4.06 s, ~4 min to converge).
- Zero `failed to adjust surb balancer` lines on the client for the session.
- **Cold-start delay bubble.** One-way delay during the first 15 s of load stays under a threshold, and no one-second delivery burst exceeds ~2× the send rate. A fixed exit still queued ≈8 s in hoprd's `2 × target` session egress buffer while the target was low, then drained 1 253 packets in one second. Record max delay and the largest burst; they are the residual of the shaper, not of the bridge.
- **Session hygiene after `disconnect`.** After a 60 s grace the exit logs no `keep-alive request for an unknown session` for the closed session id. A closed session's keep-alive stream kept running for 16 minutes into the next session on 2026-09-13 (772 rejected keep-alives), spending forward packets and SURBs.

**Diagnostic to keep at hand.** A histogram of inbound session read lengths from the client (the `instrument.diff` build) separates the two cold-start failure modes at a glance: reads of exactly `frame_size` (1500) are packed datagrams, short reads with decapsulation failures are loss.

**Why it exists.** The whole investigation started here. A cold start collapsed the tunnel entirely (100 % decapsulation failures, `DecapStalled`, a reconnect loop) while a warm start was lossless. A suite that only ever measures warm sessions cannot see any fresh-session defect, and the two worst defects we found were both fresh-session defects.

## T08-relay-attribution: Return-path relay attribution  ·  **gate**


**Question.** Which relay is actually carrying the return traffic, and is the distribution what the topology implies?

**Method.** Parse the client's path-planner debug output over a run and count return paths per first-hop relay. Report the split and any "diversity collapsed" warnings. Count from the planner's `resolved return path … path=validated path [relay, destination]` lines at `hopr_transport::path=debug`. The debug target writes ~50 MB/min while connected; scope it to the cell and turn it off afterwards.

**Parameters.** `SUITE_EQUAL_LATENCY`=1, `SPLIT_TOL_PCT`=60 %, `SPLIT_MIN_PATHS`=1000 (below it the split is recorded, not gated); one download of `BYTES` after a 10 s idle (floored to 25).

**Pass criteria.** WARN, not FAIL, when the client log has no `resolved return path` lines. Membership: PASS iff the first-hop relay of every resolved return path is an `Open` outgoing channel peer of the exit (node 0) or the exit itself. Split: with ≥ 2 return relays and `SUITE_EQUAL_LATENCY`=1, PASS iff skew = 100·(max−min)/sum ≤ `SPLIT_TOL_PCT`=60 %; with unequal latency or fewer relays the split is RECORDED. The tolerance is 60 %, not near-even, because the split varies by release and by session (on the reference stack about even on some runs and about 86/14 on others: 9711/8254, 7369/5324 and 9627/7882 passed; 10764/1822 and 10800/1797 failed), and `SPLIT_TOL_PCT`=60 fails an 80/20 split and beyond (85/15 is skew 70): the gate is expected to fail on some healthy runs and is kept as a signal by decision. Membership is the absolute assertion; the split is a coarse guard on top of it.

Note: an earlier draft of this entry said the `resolved return path` line prints the destination "not its relay" and must never be used. Checked against a live client log, that is too strong and the reason matters. The line carries **both** fields:

```
resolved return path direction="return" destination=0x74d5…a237 index=0 path=validated path [0x68e2…f25b]
```

`destination=` is a 32-byte **offchain packet key**; the relay is the 20-byte **chain address** inside `path=[…]`. The published misreads came from reading `destination=` as the relay, not from the line being unusable. So: **parse the `path=[…]` bracket by field**, from either the candidate or the resolved line, and never pattern-match the raw line: the two fields are different lengths and different key spaces, and a regex over the whole line silently mixes them.


**Mechanics to assert.** The candidate return relays are exactly the exit's **outgoing** channels in `Open` state (a `PendingToClose` channel drops out of the planner within a minute); the client draws a relay per SURB batch (indexes 0 and 1 of a session request can differ), so one dead candidate fails roughly half of all health checks and sessions. The test should therefore also read the exit's outgoing channel set and fail if it contains anything the scenario did not declare.

**Why it exists.** The split turned out to be ~85/15 on one version line and ~51/49 on another, which is a real behavioural change (upstream had explicitly reworked return-path diversity) that no throughput number reveals. It also guards against a favourite wrong conclusion: attributing a regression to "the second relay" when 85 % of return traffic never touched it.

## T09-impairment-ladder: Return-relay set topology  ·  **gate**


**Question.** Given a fixed forward path, how does the *set* of relays the exit can use for return traffic affect throughput and integrity?

**Method.** The exit's two return relays are impaired live with `tc netem` on the host, on the relays' P2P ports, with no cluster restart and no channel change (a close and reopen inside one run leaves the channel `PendingToClose` and wedges every later test). Six cells, each on a fresh session: `equal-<ms>` delays both relays by a rung of `RUNGS`, `gap-<ms>` delays relay 1 only, and `far-<FAR>ms` pushes relay 2 alone to `FAR`. Each cell runs a 3 Mbit/s download stream of `STREAM_S` on the fresh session first, then the T04-fixed-throughput transfers; the stream's loss, the transfer completions and the client-log counters go into the row.

**Parameters.** `RUNGS`="0 25 50" ms one-way (`--fast` and `--very-fast` "0 25"), `FAR`=100 ms, `STREAM_S`=120 s (`--fast` 90, `--very-fast` 8); per cell 3 transfer reps (`--fast` 2) plus a 3 Mbit/s download stream of `STREAM_S`. SKIP unless `CLUSTER_SIZE` ≥ 3 and running as root.

**Pass criteria.** Equal cells (every relay delayed by the rung) are gates: PASS iff `reassembly_failed` = 0 and `reconnects` = 0. Gap cells (only relay 1 delayed) and the far cell (relay 2 at `FAR`=100 ms) are RECORDED. Stream loss and transfer completion are reported, not asserted.

**Why the stream runs first.** Until 2026-09-20 the stream ran after the transfers, and every equal rung failed in three runs on hoprd 4.1.3 with client 0.96.3 (fullrun5, fullrun6, rerun1): the transfers completed at 10-13 Mbit/s, but the stream that followed them on the same session showed 9 to 54 reassembly failures and 0.7 to 71 % loss at 0 ms of impairment, while T06-realtime-udp's identical stream on a fresh session read 0.2 %. The ladder could not measure latency through that. The stream now runs on the fresh session, as T06-realtime-udp's download arm does, and the post-bulk state is T06-realtime-udp's own `dl-after-bulk` arm.


**Why it exists.** This was the cause of the first self-hosted exit's poor 1-hop performance. The default strategy had opened channels to production exits in Sydney, Seoul, São Paulo and London, so the client's SURBs spread over a 20–340 ms RTT mix: 0.2 Mbit/s downloads, 300 frame discards. Pinned to one near relay: 4.6–6.3 Mbit/s, 0 discards. One far relay alone (336 ms): download merely halves, 0 discards. Near + far together: 331 discards, 158 reassembly failures, **upload collapses to 0.34 Mbit/s**. Two far relays of equal RTT carried 3 Mbit/s cleanly. RTT *mismatch* is the defect, not distance.

**testenv notes.** A local cluster has no RTT spread by default, but the `hoprd-localcluster` in `hoprnet/hoprd` can inject it natively: `--latency config:<yaml>` shapes each directed link `src → dst` (fixed, uniform or normal delay) through per-node userspace UDP relays that are announced on chain in place of the real ports, no `NET_ADMIN` and no `tc`. That gives near/far relay sets on one host. Per-test rendering of the exit's allowlist (extension 7) is still needed; the localcluster's `--channel-management api|strategy|both|none` decides who opens channels.

## T10-forced-reconnect: Forced reconnect under live load  ·  **gate**


**Question.** When the tunnel is torn down mid-call and rebuilt, does the fresh session survive traffic that is already arriving, and how long is the user dark?

**Method.** Start a T06-realtime-udp bidirectional call. At +`T_KILL` s force a reconnect from **outside** the client: remove the client's WireGuard peer on the exit server (`wg set <if> peer <key> remove` inside the exit-server container), or close the session at the exit. Two arms: **T**, where the far end keeps streaming through the reconnect, and **S**, where the far end pauses while the client is silent and resumes on its next packet (T06-realtime-udp's idle-pause control). Record: time from teardown to `session is ready`, to the first delivered downstream datagram, and to full rate; `DecapStalled` count; largest inbound read; the length of each retry cycle (client log: `Ping timed out` → `restarting connection worker` → new session); probe rebinds. At least three repeats per arm.

**Parameters.** `DUR`=300 s call (`--fast` 150, `--very-fast` 130), `T_KILL`=60 s (`--very-fast` 20), `RECOVER_MAX`=90 s, `REPEATS`=3 per arm (`--fast` and `--very-fast` 1). `DUR` − `T_KILL` must exceed `RECOVER_MAX`: with a working liveness ping the client notices a removed peer only after three ping cycles, about 75 s, so a 70 s window reads as "never recovered" (seen at `--very-fast` on 2026-09-19).

**Pass criteria.** The peer removed on the exit is the client's own, found in the exit's `wg show wggvpn dump` by allowed-ips = the client's tunnel address (the client's WireGuard is userspace over TUN, so `wg show` inside the container shows nothing), never every peer, so other clients' sessions are untouched; a repeat whose key cannot be read or whose removal fails FAILs. Per arm (T: the far end keeps streaming through the kill; S: it pauses while the client is silent) and repeat: PASS iff the first downstream packet after the peer removal arrives within `RECOVER_MAX`=90 s and `DecapStalled` = 0; a repeat that never recovers FAILs. Reconnects and probe rebinds are reported, not asserted.


**Why it exists.** This is the user's "intermittent loss of connection". On 2026-09-14 every one of ten reconnects died 3–7 s after `session is ready` (`DecapStalled`), then cost a 2-minute `Ping timed out` wait and a worker restart, about 3 minutes per cycle, ~70 % of the hour lost; arm S (quiet restart) had zero failures. On the datagram-relay exit the same reconnect under full load recovered in 4 s. T07-cold-start covers only the *first* connect and T23-sustained-soak only reaches a reconnect after ~30 minutes; neither exercises the reconnect path deliberately.

**testenv notes.** Needs `wg` inside the exit-server container (or an API to drop a session); no new extension otherwise. Bind the probe to the interface and re-bind on `IFCHANGE` (harness table).

## T11-capability-matrix: WireGuard session capability matrix  ·  **gate**


**Question.** Does the tunnel preserve datagram boundaries and behave as expected under each session capability set the client can request?

**Method.** Cells over `[connection.wg] capabilities`: the default `Segmentation | NoDelay`; `Segmentation` without `NoDelay`; the default plus `NoRateControl`. Each cell restarts the client with the capability set, connects, runs one `CALL_S` call and polls the exit's `/metrics` `POLL_N` times for per-session `hopr_surb_balancer_*` series, which exist exactly while the exit shapes a session (the `spawning exit SURB balancer` log line is not emitted at the default node log level, so the gate reads metrics, not logs). Decapsulation errors come from the client log.

**Parameters.** `CALL_S`=60 s (`--fast` 30, `--very-fast` 8), `POLL_N`=10 shaper polls at 2 s (`--very-fast` 4). Cells: `["segmentation", "no_delay"]` (shaper expected), `["segmentation"]` (expected), `["segmentation", "no_delay", "no_rate_control"]` (not expected).

**Pass criteria.** Per cell: PASS iff `decap_error` = 0 and the number of per-session `hopr_surb_balancer_*` series on the exit rose during the session exactly when a shaper is expected for that cell. Call loss and download rate are reported, not asserted.


**Why it exists.** The exit's datagram mode on `release/4.0` keys off the *client's* `NoDelay`, and the upstream datagram-copy PRs rely on that; a client that drops the flag gets frames cut at `frame_size` again. `no_rate_control` measured inert for throughput on 2026-09-02 but removes the shaper, so a fresh session then *drops* egress when SURBs run out instead of shaping. Config-only per cell, minutes each, and the only test that would catch a capability-dependency regression on either side.

## T12-balancer-sweep: SURB balancer profile sweep  ·  **gate**


**Question.** How do the client's SURB balancer profiles (ping tier, main tier, organic minting) move connect time, cold-start behaviour and steady-state throughput, and where does the sweep collapse?

**Method.** Cells over `[connection.surb_balancing]`: main tier `max_surb_upstream` over `UPSTREAMS`, plus one cell with the raised ping tier (10 MB / 16 Mb/s). Each cell restarts the client with the config, connects (recording the connect time), runs one cold download and one warm download, and reads the exit's session target. Even passes run the cells in reverse. The masking cell then compares a cold download on the default ping tier with one on the raised tier.

**Parameters.** `UPSTREAMS`="12 16 48 96" Mb/s (`--fast` "16 96", `--very-fast` 16), `PASSES`=2 (`--fast` and `--very-fast` 1; even passes run the cells in reverse). Cells: `main:<U>` for each upstream and `ping:10MB`; each cell is a client restart, connect, one cold and one warm download.

**Pass criteria.** PASS iff the best warm download > 0 and the worst warm download across cells ≥ 0.3 × the best. Masking cell: PASS iff a cold download on the default ping tier completes within `CAP` (n = 1); FAIL when it does not but the raised ping tier's does.


**Why it exists.** 2026-09-02: 48 Mb/s ≈ baseline and **96 Mb/s collapsed downloads** (established, then flatlined under a `frame discarded` storm: the SURB flood congests the path). 2026-09-14 `coldmain`: the 10 MB ping tier alone made the cold start pass, at the price of a ≈4 900-SURB readiness gate (≈30 s at 512 kb/s). And the raised ping tier is exactly the tuned config that masked the 0.96.1 ramp bug for a day (the masking cell below). The axis has a known cliff and a known mask.

## T13-mtu-sweep: Tunnel MTU / datagram-size sweep  ·  **gate**


**Question.** Does the size of the datagram the client emits change what the session does?

**Method.** Force the tunnel MTU immediately after connect (`ip link set dev <wg-iface> mtu N`) and run a T04-fixed-throughput triple plus one T06-realtime-udp stream per cell. Cells at least: default, and a value small enough that one datagram fits one HOPR packet (≈940 B on our build). Assert the applied MTU from the interface, not from the config.

**Parameters.** `MTUS`="1420 1280 940" (`--very-fast` "1420 940"), `STREAM_S`=120 s (`--fast` 120, `--very-fast` 10), `FIXED_BY`=`hoprnet#8392`, `MTU940_DOWN_MIN_MBIT`=6; per MTU `REPS` transfers (`--fast` 2) plus a 3 Mbit/s upload stream of `STREAM_S`.

**Pass criteria.** PASS iff `reconnects` = 0 at every MTU; FAIL names the MTUs that reconnected, and says so when the shape is the old mechanism (1420 and 1280 reconnect, 940 clean). The download median at MTU 940 must be ≥ `MTU940_DOWN_MIN_MBIT`=6 Mbit/s. Until 2026-09-20 this was an XFAIL tagged `hoprnet#8392`; on hoprd 4.1.3 fullrun5 recorded XPASS at every MTU and T24-sustained-upload ran 900 s at MTU 1420 with 0.0 % loss, so the overflow is not in the tested stack and the gate is plain. Whether `hoprnet#8392` itself or the hopr-lib bump in that tree removed it was not confirmed from the git history; `FIXED_BY` stays as the mechanism's name.


**Why it exists.** MTU is not a tuning knob here, it is a **mechanism switch**. At ≤ 940 B a datagram fits a single HOPR packet, so no opportunistic second SURB is minted, and that alone converted the sustained-upload death at +371 s into a full 10-minute run with 0.05 % loss, 0 size evictions and no reconnect, while **throughput did not change either way** (800 MB uploads at 9.15 and 9.75 Mbit/s, both far past the previous death point). A sweep that only reports Mbit/s would have called this a null result.

## T14-novpn-baseline: No-VPN baseline  ·  *diagnostic*


**Question.** Is the traffic target itself healthy and fast, independent of the VPN?

**Method.** Identical to T04-fixed-throughput (same payload, cap, reps, target) but issued outside the tunnel. Reports the same fields.

**Parameters.** `REPS` transfers of `BYTES` from the client container straight to the target, tunnel disconnected.

**Pass criteria.** PASS iff all `REPS` downloads and all `REPS` uploads complete; as a diagnostic, a miss is reported as WARN.


**Why it exists.** It is the control that makes a slow cell attributable. Ours showed the target delivering ~198 Mbit/s and 1.9 ms RTT, so every tunnelled figure below that was the VPN's. Without it, "the target got slow" stays a live alternative explanation for every regression.

**testenv notes.** Trivially runnable: execute the same script from the host, or from the client container before connecting. Should be run **once per suite invocation**, not once per cell.

## T15-warmup-knee: Post-connect delay sweep  ·  *diagnostic*


**Question.** How long must a fresh session idle before it can carry full-rate traffic?

**Method.** One download per idle delay in `DELAYS` after connect (ramp wait opted out), holding everything else fixed. Plot first-transfer throughput against delay.

**Parameters.** `DELAYS`="0 5 15 30 60" s (`--fast` "0 5 20", `--very-fast` "0 5"), `KNEE_FRAC`=0.8, `KNEE_MAX_S`=30 s; each delay is a fresh connect with `ramp_wait_opt_out=True` followed by one download.

**Pass criteria.** none. RECORDED. Knee = the first delay at which the first download reaches `KNEE_FRAC`=0.8 × the best delay's rate; a second record flags a knee beyond `KNEE_MAX_S`=30 s as a ramp defect.


**Why it exists.** The generalisation of T07-cold-start, and the test that would have characterised the client bug in one run: the knee sat at ~240 s, which is exactly the broken ramp's length. It converts "cold starts are bad" into a number a developer can match against a constant in the code.

## T16-metric-sampling: Exit/node metric sampling during load  ·  *diagnostic*


**Question.** What is the node doing while the client sees a problem?

**Method.** Sample each node's Prometheus endpoint at 1 Hz for the duration of a T04-fixed-throughput run and emit JSONL: packets sent/received/forwarded, mixer queue depth, mixer average delay, and per-session SURB balancer estimate/target/rate. Summarise per run window into rates and min/median/max.

**Parameters.** node sampler at 1 Hz; one download of 2×`BYTES` and one upload of `BYTES` after a 5 s idle (floored to 25).

**Pass criteria.** none. RECORDED: the exit's balancer target and estimate maxima (`hopr_surb_balancer_current_buffer_{target,estimate}`, max over sessions; the client-side names were read until 2026-09-20 and always returned None), per-node egress-drop and rejected-count deltas, forwarded and sent packets/s maxima, node and container CPU, the balancer-level-vs-throughput correlation and slope, and the exit's health-check session count.


**Correlate, do not eyeball, the client's balancer.** Sample the client's SURB buffer estimate at 1 Hz through a download and correlate it with per-second throughput. On 2026-09-12 the balancer was "under pressure" (buffer below target on ~40 % of ticks, control output active) yet the median download speed was the same below and above target (5.0 vs 5.4 Mbit/s, r = 0.19). A stressed balancer is therefore not evidence of a SURB-limited download; only the slope is. The same rule applies to any gauge that looks alarming in isolation.

**Sample the relays, not only the exit.** Per relay container at 1 Hz: CPU, forwarded packets/s, `hopr_egress_ring_buffer_dropped`, `hopr_packet_rejected_count{reason}`, `hopr_rayon_queue_wait_seconds`, and count of `decode timeout` / `balance of channel … too low` log lines. Correlate every client-side RTT spike (> 500 ms) with the relay CPU series. On the DO triad every RTT spike at 1.5 and 3 Mbit/s mapped onto a 10–20 s CPU burst on the *public* relay at a 5-minute wall-clock boundary (foreign peers' synchronised traffic, 2–3× baseline, forwarding drops meanwhile); the exit had none. A relay that is also public duty is a measurement hazard, and this is how it shows.

**Further assertions and telemetry traps**.

- Exit `failed to resolve routing error=surb: no surb for pseudonym` count must be **0** during load. This is **download-side SURB starvation**, a different defect from T24-sustained-upload's upload-side opener overflow: a 6 Mbit/s download of 1 200-byte datagrams needs ≈1 250 SURBs/s delivered up the forward path; on 2026-09-13 the supply fell behind, the exit's `sent/s` decayed 1 264 → 803 → 15 and the session stayed dead with the tunnel nominally up (no reconnect with a tolerant watchdog). Pair it with T18-capacity-ceiling's rate ladder so the SURB-supply ceiling is recorded per version.
- Also record the balancer's control output / per-session `max_surbs_per_sec` (the shaper cap) and, where the datagram relay is deployed, the forwarder's end-of-session `session_to_stream_datagrams` / `stream_to_session_datagrams` counters.
- Session-labelled telemetry **gauges retain up to 2 000 closed sessions** (snapshots up to 12 h old); filter on `hopr_session_lifetime_state == 0` before averaging, or the SURB buffer is an average over days of dead sessions: the sampler's `hopr_surb_balancer_*` sum over retained sessions is not the live level. `nerd-stats` `rtt_ms` is not a live measurement (it read 439 on every sample of a 60 s probe; use `status` `ping_rtt`). `estimated_loss` is a decaying running average: read its onset, not its level.

**Why it exists.** This is what proved the exit was *not* the bottleneck in the hoprd regression: during bad runs the exit sent 380–560 packets/s versus 830–860 in good runs, with a mixer queue under 400, a 9.5 ms mixer delay and a SURB buffer that never starved. It also measured the client ramp directly: the balancer target climbing 147 units every 4.06 s, which is the bug in one line.

**testenv notes.** testenv already runs otelcol + VictoriaMetrics, so this can be a PromQL query over the run window instead of a bespoke sampler, which is better because it is already wired. Note that hoprd exposes **no packet-drop counters**; absence of a drop metric is not absence of drops.

**Join amendment**. Sample *every* host on one time axis (client probe trace, exit and relay node samplers, ICMP pinger, WireGuard counters, hoprd metrics) and join them, rather than reading each in isolation. That join is what produced the only mechanical explanation of the residual stalls: every RTT spike above 500 ms mapped onto a hoprd CPU burst on the relay at a **5-minute wall-clock boundary**, 2–3× baseline for 10–20 s and up to 650 % of one core, which is foreign traffic arriving on a public relay rather than anything the test did. Treat relay CPU as a required covariate (ours ran 280–360 % for ~2,000 forwarded packets/s), and beware that a first-pass attribution column is not literal: ICMP to a client behind the kill switch always times out, so that cause must be excluded by construction and the baseline taken from the healthy window only.

## T17-latency-matrix: Node-to-node latency matrix and peer survey  ·  *diagnostic*


**Question.** What are the real RTTs between the controlled nodes, and what does the network around them look like?

**Method.** hoprd's `/peers/{addr}/ping`, 10 pings per ordered pair among all controlled nodes (exit, relays, client), then 10 pings from one relay to every peer in its `network/connected` table. Report min / median / mean / max / stdev per pair, and per peer with its announced IP.

**Parameters.** `N`=10 pings per ordered pair, `STDEV_MAX`=25 ms (declared, not asserted), `SUITE_LATENCY_MAP`="" , `LATENCY_TOL_MS`=15.

**Pass criteria.** With no impairment map: RECORDED. With `SUITE_LATENCY_MAP`="idx=ms …" the test becomes a gate: PASS iff for every entry the node0→idx median is within `LATENCY_TOL_MS`=15 ms of ms + the node0→node1 median, or is at least ms.


**Why it exists.** Cheap (a few hundred pings), and it identified the saturated relay of T01-topology-preconditions's forwarding probe by its jitter tail before the probe existed. It is also what tells a test author which impairment rungs are realistic.

**testenv notes.** Runs as-is against the localcluster API; in a local cluster the interesting output is the stdev, not the median.

## T18-capacity-ceiling: Exit and relay capacity ceiling  ·  *diagnostic*


**Question.** How many HOPR packets per second can one exit and one relay carry, and at what CPU cost?

**Method.** Single client, T06-realtime-udp rate ladder in one direction until delivery collapses, sampling packets in+out and CPU per hoprd container at 1 Hz (T16-metric-sampling). Report the knee in packets/s, the CPU per 1,000 packets/s, and the queueing signature before the knee.

**Parameters.** `LADDER`="1 2 4 8 12 16" Mbit/s (`--fast` "2 4 8 12", `--very-fast` "4 8"), `STEP_S`=60 s per rung (`--fast` 30, `--very-fast` 12); one session, a download stream per rung.

**Pass criteria.** none. RECORDED. Knee = the last rung with loss < 5 %; watchdog reconnects on rungs below the knee are recorded individually and those above it are summed. The reference stack's knee was 8 Mbit/s.


**Why it exists.** Measured: the exit's hoprd tops out at ≈ 2,000 packets/s in+out (≈ one core), which is why 6 Mbit/s of 1,200-byte packets queues to 5–15 s on every relay set; the relay spent 280–360 % CPU (4 vCPU) forwarding ~2,000 packets/s. Both numbers are the denominators every other test divides by.

## T19-background-load: Contending background load ("the trickle test")  ·  *diagnostic*


**Question.** How much does *any* other activity on the same exit cost the client doing real work?

**Method.** Two clients on one exit. The active one runs a T04-fixed-throughput transfer; the neighbour is idle in the control arm, then fetches a small object (`TRICKLES`: 10 kB, then 100 kB) every 2 s in the load arms. Report the active client's throughput and completion per arm.

**Parameters.** `TRICKLES`="10 100" kB fetched every 2 s by the neighbour (`--very-fast` 100), `STEP_FRAC`=0.5; SKIP unless `CLIENT2` is running.

**Pass criteria.** As a diagnostic it never fails the run. WARN when the idle control does not complete, or when any trickle arm's download is incomplete or below `STEP_FRAC`=0.5 × the idle control's rate (the step change the test exists for); PASS otherwise, always recording client 1's rate with an idle neighbour and under each trickle size with its ratio to idle.


**Why it exists.** Measured on four 2-slot exits: an idle neighbour let the active download finish at 4.4–6.6 Mbit/s; a 10 kB/2 s trickle cut it to 1.4–2.0 and it hit the cap, and **100 kB every 2 s, ten times the bytes, changed nothing further**. Uploads were untouched (7.8–8.5 under load). The scarce resource is return-path capacity spent *per request*, not per byte, which no per-byte capacity model predicts. It is also the most realistic multi-user scenario there is, and far cheaper than T22-concurrent-clients.

## T20-fault-injection: Relay fault injection: loss, drop, restore  ·  *diagnostic*


**Question.** What happens to a live session when a relay degrades or disappears, and does it recover?

**Method.** Establish a session under steady T06-realtime-udp load, then at a fixed offset impair **one** relay of the return set: a `tc netem` loss ladder (1, 5, 20 %), then a full pause of the relay process (SIGSTOP for `STEP_S`), then restore. Record time-to-detect, whether the session survives, time-to-recover, and whether the *forward* direction degrades too.

**Parameters.** `LOSSES`="1 5 20" % (`--very-fast` 5), `STEP_S`=60 s per step (`--fast` 30, `--very-fast` 12), `RELAY`=1; the call lasts (1 + number of loss values + 3) × `STEP_S`, which is the ladder's own length (until 2026-09-20 it was computed from the length of the `LOSSES` string, so the call outran the script and every run printed an empty call loss); SKIP unless running as root, SKIP when the cluster status carries no pid for `RELAY` (a SIGSTOP of pid 0 would stop the runner's own process group), and SKIP naming the step when any `tc` qdisc or filter cannot be installed (an unimpaired tunnel would otherwise survive and PASS).

**Pass criteria.** Each rung's `tc qdisc change` is checked and recorded (`applied` per row); WARN when the session survived but any rung was not applied, because the ladder measured was not the one configured. Otherwise PASS iff the client is still connected after the loss ladder and a `STEP_S` SIGSTOP of the relay; as a diagnostic, a miss is WARN. Call loss, stalls and reconnects are reported, not asserted.


**Why it exists.** T09-impairment-ladder varies latency and relay-set membership between sessions. Neither touches a fault arriving mid-session, which is the real-world case. Upstream documents this path as hazardous in both directions: killing a return relayer can collapse even a 0-hop forward direction, because the entry's SURB estimate cannot distinguish a dead return path from an idle peer; and the automatic recovery actions are destructive on a false positive (one dropped a healthy 100 % baseline to 1.3 %, both together to 0.14 %). A test that never injects a fault cannot see either behaviour, and the `PendingToClose` floor above means the drop arm must be planned, not improvised.

## T21-passive-observer: Independent passive observer  ·  *diagnostic*


**Question.** When the client says the network is broken, is it the network or the client?

**Method.** Run a second, passive client that never connects: it only polls destination health and records what it sees, on its own host, for the duration of the active test. Compare the two views.

**Parameters.** `DUR`=300 s (`--fast` 60, `--very-fast` 20), one sample every 15 s; SKIP unless `CLIENT2` is running.

**Pass criteria.** PASS iff the active and passive clients disagree on the count of Ready destinations in at most 1/5 of the samples.


**Why it exists.** It settled a standing belief. The active node was reporting most exits as merely `Routable` with "Connection reset by peer" on version and health checks, which reads as a broken network, while an independent passive node saw the *same* exits as `ReadyToConnect` throughout. The fault was local. Without the observer there is no way to tell that apart, and the wrong conclusion ("the exits degrade") had already been written down. It is also nearly free and can run during every other test in this catalogue.

## T22-concurrent-clients: Concurrent client ladder  ·  **gate**


**Question.** How does the stack behave as simultaneous clients increase?

**Method.** Ramp N concurrent clients, each running T04-fixed-throughput, recording aggregate and per-client throughput plus the per-client error counters, and the point at which any client fails to connect.

**Parameters.** `LADDER`="1 2 4" clients, `TOL_PCT`=25 %, `AGG_MIN_MBIT`=8, `WARMUP`=1 with `WARMUP_BYTES`=500000, `CAP` (`--very-fast` 45 s); SKIP unless at least 2 client containers are running.

**Pass criteria.** Per rung: FAIL if any client fails to connect or does not complete its `BYTES` download within `CAP`; the FAIL names, per incomplete client, its bytes and HTTP code, its warm-up bytes, whether it was still Connected right before the measured transfer, its reconnects and tunnel-ping timeouts, and the Disconnect commands it received during the rung (all of it is in the per-client row too). Across the ladder, only when every rung completed: PASS iff no higher rung's aggregate falls more than `TOL_PCT`=25 % below any lower rung's aggregate (one-sided: aggregate rising with concurrency is the healthy shape on a stack where one client cannot saturate the exit; rerun1 on 2026-09-20 measured n=1 10.6, n=2 13.7, n=4 16.1 Mbit/s with everyone complete, and the earlier "every rung within 25 % of the best rung" rule failed that ladder as a 34 % drop at n=1). Fairness (slowest client over the rung mean) is recorded, not asserted: starvation shows as an incomplete transfer, and a fairness bound from one ladder would be a number nobody can justify. The top rung's aggregate must be ≥ `AGG_MIN_MBIT`=8 Mbit/s.


**Why it exists.** Earlier ladders located a hard break at a specific client count and a throughput plateau well below the no-VPN baseline, neither visible in a single-client test. Numbers from the 2026-09-09/10 DO ladders on the Austria exit: aggregate plateau 38–49 Mbit/s from n≈7 (22–28 after a node change), the exit stopped admitting between 14 and 16 clients, zero-byte transfers from n=12; the same 16 clients without the VPN shared ~220 Mbit/s gracefully, so the rig is not the plateau.

**Variant: asymmetric interference.** One bulk client (T04-fixed-throughput) plus one *trickle* client fetching 10 kB every 2 s through the same exit. On the public NL/UK exits the trickle cut the bulk client's download by ~70 % (4.4–6.6 → 1.4–2.1 Mbit/s) and 10× more trickle changed nothing; uploads were untouched. That is return-path contention and no equal-load ladder shows it.

**Also record** each exit's advertised session `slots` before the ladder (most production exits publish 2, one publishes 16) and assert admissions never exceed it (a ladder above the slot count has exactly one exit it can run against), and exit availability *after* the ladder: the Austria exit stayed `Routable` but refused tunnels for hours after each 16-client ladder. A ladder is not over when the last client disconnects.

**testenv notes.** Runs on the client containers `just clients-start` brings up (`CLIENT_COUNT`, each on its own pre-funded identity from `EXTRA_IDENTITIES`); SKIPs below two.

## T23-sustained-soak: Sustained soak  ·  **gate**


**Question.** Does a stack that passes a 3-minute test still pass after hours?

**Method.** Continuous mixed load for `DUR`: a 1.5 Mbit/s echo call for the whole duration plus one download and one upload of `BYTES` every `INTERVAL`, logging the worker's RSS and the reconnect count at each interval. Container logs are size-capped (`LOG_MAX_SIZE`) so the run cannot fill the disk.

**Parameters.** `DUR`=3600 s (`--fast` 600, `--very-fast` 60), `INTERVAL`=300 s (`--very-fast` 30), `LOG_MB_MIN_MAX`=200 MB/min, `CALL_LOSS_MAX`=5 %, `CALL_RATE`=1.5 Mbit/s, `SAMPLE_MIN_PCT`=80; a `CALL_RATE` echo call for `DUR` plus one `BYTES` download and upload every `INTERVAL`. The session is covered by `client.deadman_cover(DUR + INTERVAL + 2·(CAP + 30))`, because the loop overshoots `DUR` by up to one interval and a transfer pair: fullrun5 ran the hour under the 900 s deadman default, the client was disconnected at +15 min, every later transfer read 0 Mbit/s, the call delivered 24 % (= 900/3600) and the verdict was PASS, because a deadman disconnect is not a reconnect. That was the "soak death" of 2026-09-20 in the suite; it was the harness.

**Pass criteria.** FAIL `UNMEASURED` when the call sent fewer than `SAMPLE_MIN_PCT`=80 % of its expected `CALL_RATE`·10⁶/8/1200·`DUR` packets (a send the kernel refused on a missing interface counts as `send_failed`, never as sent or as loss); otherwise PASS iff `reconnects` = 0 and final worker RSS < 2 × initial RSS + 200000 kB and client log growth < `LOG_MB_MIN_MAX`=200 MB/min (the client runs at path-planner debug for T08-relay-attribution and writes ~70 MB/min; the gate is for hot loops, 1.6 GB/min in the incident that motivated it; earlier runs read 0.6–2.4 MB/min only because reconnects recreated the log file) and the call's loss < `CALL_LOSS_MAX`=5 % (T06-realtime-udp's bound for the same probe at the same rate; a missing call report counts as 100 %). Stalls and reassembly failures are reported, not asserted.


**Why it exists.** The defect that started the investigation only appeared ~30 minutes into a clean call, as an opener-cache starvation that then produced a reconnect loop. Short tests were green throughout. Also the test that caught a relay silently rejecting tickets for 2.5 hours, which had been quietly contaminating every measurement in that window.

**testenv notes.** Needs bounded log capture (`--log-opt max-size`) on every container; the investigation lost a host to a full disk before that was in place.

**Idle footprint and log rate.** Sample CPU and log growth per minute on **every component** (client, exit and relays) at the configured log level, idle and under load, and assert both stay under a threshold. The exit at `hopr_transport_session=debug` wrote ≈4 MB/s during a 250 pps call and needs a docker log cap. The 0.94.1 client with `hopr_transport_p2p=debug` entered a discovery hot loop (`DialFailure … NoAddresses` for ~130 peers, round-robin) that ran the worker at 140 % CPU and wrote **1.6 GB/min**, 34 GB in a few hours; an earlier fleet run at debug wrote ~15 GB/h idle and filled 16 disks. The check is cheap and it is the only thing that catches a logging or discovery regression before it takes the host down.

## T24-sustained-upload: Sustained one-directional upload (organic-SURB overflow)  ·  **gate**


**Question.** Does a long, steady upload survive, and does the client keep being able to open the replies?

**Method.** Upload-only UDP (`streamprobe.py`) at `RATE` for `DUR` with 1200 B datagrams, reading the client's `hopr_packet_rejected_count{reason="undecodable"}` before and after through `gnosis_vpn-ctl -o json telemetry`; then the same with the tunnel MTU set to 940 so a datagram fits one HOPR packet. Not implemented from the original design: the 6 Mbit/s rate, 1 Hz sampling of the exit's SURB estimate, and counting the client's `cause=Size` opener evictions and `clamped_to` warnings (the entry over-minting past what the exit can hold).

**Parameters.** `RATE`=3 Mbit/s, `DUR`=900 s (`--fast` 240, `--very-fast` 25), `MTUS`="default 940", 1200 B datagrams.

**Pass criteria.** Per MTU: PASS iff stream loss < 5 % and `reconnects` = 0 and the client's `hopr_packet_rejected_count{reason="undecodable"}` grew by fewer than 50. A missing end-of-stream report from the server is an UNMEASURED FAIL that says so and names the second the tunnel interface went down, if the probe saw it go down. The session is covered by `client.deadman_cover(DUR)`: fullrun5 ran the 900 s arms under the 900 s deadman default, the client was disconnected ~30 s before the sender finished, and both arms printed a blank loss.


**Why it exists.** Uploads died at +405 s (3 Mbit/s) and +250 s (6 Mbit/s), every time. Each full-size data packet mints an organic SURB (unconditionally in 4.0.x), the client's reply-opener store caps at 100,000 per pseudonym and evicts *oldest*, the exit spends SURBs *oldest-first*, so after ~100k packets every reply the exit sends is one the client can no longer open. 900-byte datagrams (no organic SURB room) ran two 800 MB uploads clean, which is the verification. The fix lineage is hoprnet PR #8392 (gate organic SURBs on the balancer target); a newest-first exit store would break PIX and is not the fix. The test should stay in the suite after the fix as the regression guard.

**testenv notes.** Reads the client telemetry with `docker exec … gnosis_vpn-ctl -o json telemetry` (`Client.telemetry_metric` in `suitelib/client.py`); scraping it into VictoriaMetrics (extension 8) is still open.

## T25-knob-ab: Component runtime-knob A/B  ·  *runbook*


**Question.** Does a single environment knob on a single component explain a regression?

**Method.** Restart exactly one component with one env var changed, leaving versions, configs and every other component untouched; run T04-fixed-throughput both ways; diff. The knobs that mattered for us: `HOPR_INTERNAL_IN_PACKET_PIPELINE_CONCURRENCY` (relay decode concurrency, this *was* the hoprd fix), `GNOSISVPN_SURB_RAMP_SECS` (client ramp length, from our patch), `HOPRD_SURB_POP_ORDER` (from our patch, used to *exonerate* the SURB pop order); `HOPR_SESSION_MIN_SURB_BUFFER_DURATION_MS` on the exit (the divisor of the local SURB shaper's cap, `target / duration`; 1000 ms raised a session's cap from 196 to 980 packets/s and was the 2026-09-15 workaround for client 0.96.1 never promoting its session target).

**Parameters.** `KNOB`=`HOPR_INTERNAL_IN_PACKET_PIPELINE_CONCURRENCY=<nproc×8>` applied by cluster restart, or `CLIENT_KNOB`="" applied to the client instead; `REPS` transfers per arm (`--fast` 2).

**Pass criteria.** none. RECORDED: both arms' median download and upload.


**Why it exists.** It is how both regressions were finally pinned, and how a plausible-but-wrong suspect (the exit's LIFO SURB pop order) was cleared in one run instead of being written up as the cause. Negative results from this test are as valuable as positive ones.

**testenv notes.** Cluster-node env is injected with `CLUSTER_ENV` through `just cluster-restart`; the client knob goes through `CLIENT_EXTRA_ENV`. Every node gets the same env (extension 1).

## T26-version-matrix: Version matrix across components  ·  *runbook*


**Question.** Which component's version change causes a regression?

**Method.** Cartesian sweep over {client version} × {node version}, each cell a T04-fixed-throughput run, with the run order **reversed** on a second pass so wall-clock and version are not confounded.

**Parameters.** none.

**Pass criteria.** none. RECORDED: the cell name, client image, hoprd binary and cluster env it runs under.


**Why it exists.** Our 2x2 was the instrument that separated "the client regressed" from "the nodes regressed", and the reversed-order repeat is what proved the effect was not drift, after I had wrongly retracted a real finding on exactly that suspicion.

**testenv notes.** Partially available: `HOPRD_DIR` / `GVPN_CLIENT_DIR` / `GVPN_SERVER_DIR` already point at sibling checkouts, so a matrix driver can re-`just build` + `just up` per cell. Needs the per-role version split of T27-role-split to be complete, and needs T02-build-provenance's provenance capture so a cell is labelled by what actually ran.

## T27-role-split: Role split: exit version vs relay version  ·  *runbook*


**Question.** Within the mixnet, is a regression in the node acting as the *exit* or in the nodes acting as *relays*?

**Method.** Run the exit node on version A and the relay nodes on version B, then swap. Two T04-fixed-throughput runs.

**Parameters.** none.

**Pass criteria.** always SKIP: the localcluster runs one hoprd binary for every node.


**Why it exists.** This was the decisive test for the hoprd regression and it inverted the working hypothesis. Everyone (me included) assumed the exit, because the exit terminates sessions, runs the SURB balancer and hosts the egress shaper. Exit-new/relays-old was the *best* cell of the night; exit-old/relays-new halved throughput in both directions. Without this test the search would have stayed on the exit indefinitely.

**testenv notes.** **Not currently possible**: the localcluster builds all nodes from one `HOPRD_DIR`. See [Required extensions](#required-extensions). This is the single highest-value extension in this document.

## T28-transport-ab: Application-transport A/B (documented negative)  ·  *runbook*


**Question.** Does the application transport or congestion controller change tunnelled throughput?

**Method.** T04-fixed-throughput in three paired variants over the same sessions: HTTP/1.1 vs HTTP/3 (QUIC), immediate vs after 3 minutes idle, CUBIC vs BBR (quiche client and server). 40 sessions each, one cycle per fresh client. Alternate the pass order (ABBA) so link drift is not credited to whichever variant runs first; report a sign test on direction plus n and the per-pair range; and run the same pair once **outside** the tunnel as a control. The kernel BBR-vs-CUBIC upload win (+82 %, 13/13 pairs on the ~94 ms VPN path) reversed to −13 % (5/5 for CUBIC) on a direct 18 ms path (the effect belongs to the path, not the machine), and the download column, whose sender is remote, must stay flat or the pair is drift.

**Parameters.** `TARGET_H3_PORT` (unset).

**Pass criteria.** SKIP unless the client's curl reports HTTP3 and `TARGET_H3_PORT` is set; the h3 arm is not implemented, so the test SKIPs either way.


**Why it exists.** All three were run in 2026-09-11/12 and none is a lever: HTTP/3 downloads were half of HTTP/1.1 (median ratio 0.50) and its uploads acked 4/40 versus 36/40; after 3 minutes idle downloads were *slower* in 30/40; BBR/CUBIC was a coin flip (0.94, 15 faster / 19 slower). The SURB-metered return path sets download throughput regardless. Keeping the test as a runbook item stops the axis being re-tested from scratch on every new release.

**QUIC measurement traps**. If the HTTP/3 arm is kept, three rules, each of which produced a wrong number first. Force `--http3-only` so a failure is hard rather than a silent fallback to HTTP/2. Do not read `size_upload` for a QUIC upload as a measurement: it counts bytes handed to the transport, so an upload without a 200 response is an upper bound. And a QUIC handshake started shortly after a capped download fails while the tunnel's return path is still draining, where TCP survives the same moment: allow a generous connect timeout plus one retry after a drain, and flag the retry in the record rather than dropping the sample.

## T29-destination-sweep: Multi-destination sweep (production)  ·  *runbook*


**Question.** Which public exits accept a tunnel right now, and at what speed?

**Method.** For each destination in the client config: connect, in-tunnel ping, one T04-fixed-throughput repetition, disconnect; N cycles; one row per destination and cycle with connect success, RTT, throughput and the error counters.

**Parameters.** `CYCLES`=3 (`--fast` 1); per destination and cycle: connect after a 5 s idle (floored to 25), in-tunnel ping, one download of `BYTES`.

**Pass criteria.** PASS per destination and cycle iff the connect succeeds; RTT and download rate are recorded, not asserted.


**Why it exists.** A 40-run cycle over 8 exits found one exit that connected but carried ~0 (Brazil) and one that stayed down for hours after a ladder (Austria); a page-load campaign found the same two. testenv's `just e2e` already produces this shape per destination; on production the nightly `vpn-speedtest` does, and the two should share the row format (extension 4).

## T30-hopcount-ab: Hop-count A/B on one exit  ·  *runbook*


**Question.** How much of the loss and latency is the relay hop itself?

**Method.** Same client, same exit, same target: a T04-fixed-throughput triple at 1 hop and at 0 hops. Report both plus in-tunnel RTT.

**Parameters.** `REPS` transfers per arm (`--fast` 2); SKIP unless the `<DEST>-h0` destination exists.

**Pass criteria.** PASS iff the 0-hop median download ≥ the 1-hop median download; WARN otherwise.


**Why it exists.** It is the cheapest way to separate "this exit is slow" from "this path is slow", and it was the measurement that cleared an exit host of a fault: 0-hop gave 10.3–12.1 down / 21.2–22.5 up at 21 ms, the same exit at 1 hop gave 6.1–7.1 / 7.2–8.5 at 52 ms. **Trap:** 0-hop needs `gnosis_vpn-root --allow-insecure` (a systemd drop-in, not a config key) and production refuses it with `routing mode not allowed`, so this test belongs to testenv and self-hosted exits only.

## T31-frame-forensics: Instrumented-build frame forensics  ·  *runbook*


**Question.** When the client rejects inbound traffic, what exactly is arriving?

**Method.** Build the client with ~10 added lines that log the length and leading bytes of every inbound datagram, run the failing scenario (T07-cold-start cold start is the canonical one), then reduce: a read-length histogram, the fraction failing decapsulation, and a **slab analysis** that groups reads into runs (a sequence of `frame_size` reads ended by a short one) to reconstruct what the sender actually framed.

**Parameters.** one cold connect and one download of `BYTES`; SKIP unless the client log contains `inbound datagram` lines.

**Pass criteria.** PASS iff no packed slab is found, i.e. no run of inbound reads that sums to more than 1500 B before a short read; FAIL names the slab count and the failed-read fraction.


**Why it exists.** This is the only technique that produced *proof* rather than inference about the datagram-boundary defect. Healthy: 600 reads, 0 failed, max 1452 B. Broken: 256 reads, 95 % failed, of which **201 reads of exactly 1500 B, all 201 failing**, and the slab analysis showed each packed run was a whole-datagram slab of 14–16 kB, at or under the 16 384-byte bridge buffer. Numbers like that end an argument; counters and throughput never did. Expensive, so it belongs in `deep` or in an investigation, not in `regression`.

## T32-congestion-control: Client-host TCP congestion control A/B (CUBIC vs BBR + fq)  ·  *runbook*


**Question.** Does the client host's own TCP stack limit the tunnelled *upload*, independently of the VPN?

**Method.** Two arms on the same client, same session type, same target: `net.ipv4.tcp_congestion_control` `cubic` (stock) vs `bbr` with `net.core.default_qdisc = fq`. ABBA order, at least 13 pairs, upload is the treated direction and download is the **null-direction control** (its sender is the far end, so it must not move). Report per-direction paired ratio, sign counts and spread (rules R2–R4).

**Parameters.** `PAIRS`=6 (`--fast` 3), ABBA order, client restarted per arm with `net.ipv4.tcp_congestion_control` set to `cubic` or `bbr`; SKIP unless `bbr` is in the host's available congestion controls.

**Pass criteria.** As a runbook item its FAIL is emitted as WARN. WARN when no valid cubic/bbr pair was measured (a failed restart or connect leaves an arm at 0 and it is dropped from the pairs) or when a client restart fails, with a summary row saying the A/B was abandoned; the default client is restored whatever happens. Otherwise PASS, recording the bbr/cubic paired ratio and sign count for upload (treated) and download (control) and the number of pairs.


**Why it exists.** 2026-09-02: BBR + fq on the client host raised upload from 5.4 → 8.1 Mbit/s (NL) and 5.0 → 5.6 (USA); the nightly A/B then held at +82 % upload with 13/13 pairs agreeing (p = 0.0002) while download stayed null (+4 %, 8/13, p = 0.58). It is the only client-side no-build lever found in two weeks, and it silently inflates every upload figure recorded after the host was switched (R2 already cites it; the catalogue had no test that produces it). 2026-09-06 qualified it: the win is the path, not the machine, so it must be re-measured per exit rather than assumed.

**testenv notes.** The client container shares the host kernel; `tcp_congestion_control` is per network namespace, so `docker run --sysctl net.ipv4.tcp_congestion_control=bbr` works once `tcp_bbr` is loaded on the host (`modprobe tcp_bbr`; DO-a lists only `reno cubic` until then). `default_qdisc` is host-global.


# Merged away

These no longer exist as separate tests. Their assertions live where each line says. The catalogue was renumbered contiguously afterwards, so the old numbers now belong to different tests and are deliberately not cited here.

- **the former config-default-vs-tuned test** was merged into T12-balancer-sweep (the masking cell) and T01-topology-preconditions (the effective-config precondition).
- **the former synthetic latency ladder** was merged into T09-impairment-ladder: one impairment test over both axes, channel set x per-link latency.
- **the former watchdog-under-saturation test** was merged into T18-capacity-ceiling: the watchdog is the top rung of the capacity ladder.
- **the former channel-strategy test** was merged into T01-topology-preconditions: part (c), the client channel pin, is a precondition; (a)/(b) dropped.
- **the former reconnect-cycle endurance test** was merged into T23-sustained-soak: the worker-RSS plateau is a soak assertion.
- **the former health-check-load test** was merged into T16-metric-sampling: the health-check session count is a covariate.

# Analysis rules

Not tests: the rules that decide whether a test's output means anything. Every one of these was learned by getting it wrong first, and each names the specific claim it would have stopped.

**R1. A correlation near ±1.00 is usually an identity; report the slope.** `corr(SURB legs/s, download Mbit/s) = +1.00` is a restatement that SURB legs track downloaded bytes (651 ± 59 bytes per leg); `corr(worker CPU, download) = +0.99` is worth quoting only as its slope, ~17 CPU-% per Mbit/s. **The informative correlations here are the flat ones**: peak core at `r = −0.06` is what actually ruled out CPU, and route-health checks at `+0.10`, upload↔download at `+0.03` and discarded frames at `+0.19` are what ruled those out.

**R2. A paired A/B needs ABBA order, a sign test, n, and a null-direction control.** The congestion control result stands because the upload moved +82 % with 13/13 pairs agreeing (p = 0.0002) *while the download control stayed null* (+4 %, 8/13, p = 0.58). Without the control the same numbers are drift.

**R3. Four agreeing pairs is p = 0.125, not a result.** The same experiment read −8 % with all four pairs agreeing, then +9 % with all four agreeing, and settled at +4 % by n = 13. Agreement at small n is the most convincing way to be wrong that this project has found.

**R4. Report the spread, not only the median.** The real finding in that experiment was variance, not the mean: one arm spread 1.3×, the other 3.5×. A median-only table hides the property that mattered.

**R5. One draw per rung manufactures thresholds, and the zero rung is not automatically clean.** A reported ">50 % drop at +50 ms" was a single draw; at n = 3 per rung it dissolved into a stall lottery (partial downloads per rung 1/6, 1/6, 0/6, 3/6, 2/6, 2/6). In the same series the *unimpaired* control rung itself produced one stalled download and 186 discards. Repeat every rung, and measure the zero rung as carefully as the impaired ones.

**R6. Parse the field, not the line.** See the T08-relay-attribution correction: `destination=` is a 32-byte offchain key and the relay is a 20-byte chain address inside `path=[…]`. Three successive wrong return-relay stories came from conflating them. Any log-derived metric should name the field it reads.

**R7. Coincidence is not cause; replicate with the instrument that would show it.** A 50 s return-leg outage was attributed to a p2p connection drop seen within 1.6 s of it. The drill was repeated with p2p debug on all three hosts: no connection loss at all, and the outage happened anyway. The pairing was chance.

**R8. Derive what is not instrumented, then validate the derivation.** Two that worked: the in-flight window as throughput × RTT (median ~136 KB against a 2 MiB ceiling: throughput is window-pinned, not link-pinned, which no exposed counter says); and time-to-failure as counter arithmetic ((100 000 − 15 000) / 48 ≈ 1770 s, observed 29.8 min), validated by predicting the failure time at a different rate and hitting it.

**R9. Client belief is not exit reality.** The client's balancer reported 15 000 SURBs in stock while the return leg had been dead for ~50 s, and separately logged `believed=15031 clamped_to=150`. Treat client-side SURB estimates as a *comparative* signal only, and corroborate against the exit.

**R10. Never pool across different target sets or harnesses.** The single most expensive analysis error here: two harnesses using the same formula but different target mixes produced 5.07 vs 1.92 Mbit/s for the *same* target, and pooling their means produced a conclusion that had to be retracted twice: once for the wrong mechanism, once for the wrong direction. One harness, one target set, or do not compare.

**R11. Some telemetry fields look like measurements and are not.** Four that cost real time. Session-labelled *gauges* retain thousands of closed sessions, with snapshots hours old: filter to live sessions or a SURB-buffer average silently covers days of dead ones. The client's `nerd-stats` RTT is static, not a series: it read 439 across every sample of a 60-second probe, and 645 ms while the real ping RTT to that exit was 117 ms. `estimated_loss` is a decaying running average, so read its *onset*, not its level. And a path-length metric that counts acknowledgements is not the session's data path (80 % of it is 0-hop) and must never be quoted as a routing finding. Related: between runs the node is idle, so "the latest sample" is the wrong thing to draw: fall back to the last record that carried the field and label it, rather than widening the window.

**R12. Minted is not delivered.** A relay share computed from what the client *minted* can be off by sevenfold from what actually *arrived*: with one relay impaired, the client minted 40.6 % of return paths for the slow leg while only 5.6 % of delivered packets came that way, and the damage arrived as bursts of ~90 consecutive late packets rather than as a uniform tax. State which of the two any relay share is, and prefer the delivered mix for anything causal.

**R13. Trust the instrument over the code read.** The single most instructive sequence in this project: a coalescing claim was made from measurement, **retracted** after reading the source, then re-confirmed by an instrumented build, and the retraction was the error. The source being read was not the source that was running (the deployed node's library predated the branch being read). When a measurement and a code read disagree, instrument the running binary before believing either.

# The run

There is one run. `just suite` executes t01 to t24 in one fixed order every time; there are no profiles, because a profile you can choose is a profile someone forgets to choose. Runbook entries (t25 to t32) are in no run; start one with `just test tNN`.

**Order matters.** T03-repeatability-baseline runs third so the stack's own repeatability is on record before any gate reads a number. T05-loaded-latency runs fifth, right after the T04-fixed-throughput reference, because loaded-RTT numbers taken after an hour of other tests measure accumulated host load rather than the stack, and T06-realtime-udp runs sixth for the same reason. Everything else is ordered cheap to expensive so a broken stack fails early, and a T01-topology-preconditions failure aborts the run.

**Every test is a module in `scripts/suite/tests/` and runs by being there**; runbook items are excluded by their `KIND`, not by a list. Under the old bash runner a test had to be in one of two lists, and one in neither was dead code that still looked maintained: T19-background-load and T20-fault-injection sat in no list from the restructure until 2026-09-17, so contending-load and relay-fault coverage silently never ran.

**Shortening a run.** `--fast` substitutes the catalogue's own shorter durations (the values in parentheses in each entry) and never drops a sustained arm below 90 s. `--very-fast` cuts every long step to the shortest setting that still exercises its mechanism, 15 s arms and 2 MB transfers, takes about 50 minutes for the whole suite, and cannot see anything that needs a long session, such as a reconnect cycle on the order of a minute; never conclude "healthy" from it. `--only tNN,tNN` and `--skip tNN` are for working on one test; `--only` still runs T01-topology-preconditions first unless it is skipped explicitly, because a leftover qdisc or a missing liveness-ping alias poisons one test as surely as a run. A per-test knob is `T<NN>_<VAR>=…` in the environment (`T23_DUR=150`), and it overrides `--very-fast`.

**Open items from the liveness-ping artifact.** Measured before the server carried the alias and therefore unattributed until rerun with it: the 1-hour T23-sustained-soak going dead at +21 min, parallel flows not scaling in T05-loaded-latency, T22-concurrent-clients not completing, and the intermittent 152 s recovery in T10-forced-reconnect. No full-length (non `--fast`) pytest run existed as of 2026-09-21.

**Run ids.** Results go to `SUITE_OUT_DIR/<run-id>/` (`--run-id`, default a timestamp) with `rows.jsonl`, `verdicts.jsonl`, `provenance.json`, `summary.csv`, `run.txt`, `console.log`, `logs/` and `samples/`. A run id that already holds results is refused, because two runs once shared one and their verdicts blended into one file.

**Binary is not the goal; discrimination is.** A gate whose verdict would be the same on a broken stack is worse than a plain RECORDED, because it manufactures confidence. T12-balancer-sweep is the current example: it gates the SURB-ramp masking cell at n=1, and the cold-start collapse is intermittent, so its PASS does not distinguish a fixed ramp from an unlucky probe. Fix the sensitivity before trusting the verdict.

# Required extensions

The original list, with what testenv has since provided. Numbers are kept because entries above cite them.

1. **Open: per-role node versions and env in the localcluster** (blocks T27-role-split, completes T25-knob-ab and T26-version-matrix). All cluster nodes come from one hoprd binary with one `CLUSTER_ENV`. Needed: designate which node acts as exit versus relay and allow a different build or env per role, such as `HOPRD_DIR_EXIT` / `HOPRD_DIR_RELAY`. This is the single most informative test in this document, and it cannot run.
2. **Done: in-cluster traffic target.** `just target-start` runs the target container on the `198.18.0.0/24` network with the sized download and upload endpoint (`:8899`), the UDP echo (`:8901`), the paced stream server (`:8902`) and the two-ended call server with idle-pause (`:8903`). The services are plain Python modules (`docker/target/`); the container only places them on the far side of the exit, and the suite's self-tests run them on loopback against the probes. Any host running them serves as the target of a production-network run (`--target HOST`). A public endpoint would make every number depend on the internet path; the 2026-09-08 run through a CDN rate-limited the exit's egress IP for 51 minutes.
3. **Done: multi-client support.** `CLIENT_COUNT` client containers on pre-funded identities (`EXTRA_IDENTITIES`), started with `just clients-start`; T22-concurrent-clients runs on them.
4. **Done: run metadata and comparable output.** Every run directory holds `provenance.json` (T02-build-provenance), the effective client config, `rows.jsonl`, `verdicts.jsonl` and `summary.csv`, so two runs diff cleanly.
5. **Done: bounded log capture.** Container logs are size-capped (`LOG_MAX_SIZE`, `LOG_MAX_FILE`), every test counts errors over its own `docker logs --since` slice, and saved log slices drop the DEBUG path-planner lines unless `SAVE_LOG_RAW=1`.
6. **Done for the localcluster: impairment.** T09-impairment-ladder applies `tc netem` delay on the host toward the relays' P2P ports, and `CLUSTER_LATENCY` passes `hoprd-localcluster --latency` through for a latency map baked into the cluster. Loss and a mid-session pause come from T20-fault-injection (`tc netem loss`, SIGSTOP), with removal in a `trap`.
7. **Open, no longer needed by a test: per-test rendering of the exit's return-relay set.** T09-impairment-ladder varies the relay set by impairment instead of by channel changes, because a close and reopen inside one run leaves the channel `PendingToClose` and wedges every later test.
8. **Open: client telemetry in the metrics stack.** The suite reads `gnosis_vpn-ctl -o json telemetry` with `docker exec` where it needs a client counter (T04-fixed-throughput, T24-sustained-upload); scraping it into VictoriaMetrics at 1 Hz next to the node metrics would let T16-metric-sampling correlate it.
9. **Done: 0-hop routing.** `HOPS0_ALSO=1` generates `<dest>-h0` destinations and `CLIENT_EXTRA_ARGS=--allow-insecure` lets the client use them (T30-hopcount-ab).
10. **Done for the localcluster: mid-session impairment.** See 6; closing a channel mid-run is deliberately not done.
11. **Done: a second, passive client.** `just client2-start` (T19-background-load, T21-passive-observer).
12. **Open: a client build with the inbound-read instrumentation** (blocks T31-frame-forensics). A build flag or patch carried in testenv that logs the length and leading bytes of every inbound datagram; the test SKIPs when the running image does not emit those lines.

# What each test would have caught, in order

| Finding | Caught by | Would have been caught in |
| --- | --- | --- |
| Fresh session collapses, warm session fine | T07-cold-start | minutes |
| The collapse lasts ~240 s and scales with a ramp constant | T15-warmup-knee (knee) | one sweep |
| A tuned client config was masking the client regression | T12-balancer-sweep (masking cell) | one paired run |
| The 0.96.1 client regression itself | T04-fixed-throughput error counters + T07-cold-start | the first regression run |
| The regression is in the nodes, not the client | T26-version-matrix | one matrix |
| It is the relays, not the exit | **T27-role-split** | one paired run |
| It is decode concurrency specifically | T25-knob-ab | one A/B |
| The exit was never the bottleneck | T16-metric-sampling | same run as T04-fixed-throughput |
| Return-path distribution changed between versions | T08-relay-attribution | any run |
| The release lockfile did not describe the release binary | T02-build-provenance | one command |
| A relay silently rejecting tickets for 2.5 hours | T01-topology-preconditions | before any measurement |
| A CDN rate-limiting the exit's IP, read as an exit collapse at 8 users | harness (no CDN target) | never happens |
| A trickle from one neighbour costing the active user 70 % of the download | T19-background-load | one paired run |
| Datagram size switching a failure mechanism on and off at equal throughput | T13-mtu-sweep | one sweep |
| An exit cleared of a fault by its own 0-hop upper bound | T30-hopcount-ab | one paired run |
| "The exits are degrading": actually the local client's health-check path | T21-passive-observer | first run it rides along with |
| "Nodes degrade after many reconnects": refuted at 45 cycles, it was a keepalive | T23-sustained-soak | one endurance run |
| A client identity at 0.01 Mbit/s because it moved host and never re-onboarded | harness (identity rule) | before attributing to hardware |
| Our own target doubling as a return relay | harness (dual-role rule) | at design time |
| A published knee at +50 ms that was a single draw | T03-repeatability-baseline + R5 | one repeatability run |
| Three successive wrong return-relay stories from one misread log field | R6 + T08-relay-attribution | first parse |
| A coalescing finding retracted on a code read, then re-confirmed by instrumentation | T31-frame-forensics + R13 | one instrumented run |
| Mixed-RTT return relays collapse uploads to 0.34 Mbit/s | T09-impairment-ladder | one relay-set sweep |
| Latency *difference* only matters beyond the realistic band | T09-impairment-ladder + T17-latency-matrix | two ladder rounds |
| Uploads die at +405 s / +250 s (organic-SURB overflow) | T24-sustained-upload | first 15-minute upload |
| Exit ceiling ≈ 2,000 packets/s; 6 Mbit/s queues, watchdog kills at 250 s | T18-capacity-ceiling (incl. its watchdog rung) | one rate ladder |
| A public relay's 5-minute CPU bursts explain every RTT spike | T16-metric-sampling relay sampling | same run |
| A saturated relay pings at 6 ms but forwards nothing; 1-hop dead, 0-hop fine | T01-topology-preconditions forwarding probe, T17-latency-matrix stdev | before any measurement |
| Session opened at the ping-phase SURB target, shaped to 196 packets/s | T07-cold-start / T16-metric-sampling target assertion | first cold run |
| Client debug logging at 1.6 GB/min (discovery hot loop) | T23-sustained-soak log-rate check | minutes |
| deb postinstall re-pointed the client config | T01-topology-preconditions effective-config check | after install |
| One trickle client cuts another's download by 70 % | T19-background-load | one paired run |
| Expired worker keepalive fails every connect silently | harness (liveness row) | before any cell |
| Parallel flows in one tunnel do not scale; loaded RTT balloons to seconds | T05-loaded-latency | one session |
| Every reconnect dies in 3–7 s and loops for ~3 min per cycle | T10-forced-reconnect | first forced reconnect |
| Exit datagram mode silently depends on the client's `NoDelay` flag | T11-capability-matrix | one config cell |
| 96 Mb/s SURB upstream collapses downloads; the 10 MB ping tier masks the ramp bug | T12-balancer-sweep | one sweep |
| 6 Mbit/s download starves the exit of SURBs (`no surb for pseudonym`); tunnel stays "up" | T16-metric-sampling no-surb assertion + T18-capacity-ceiling | one rate rung |
| A moved client identity never re-announces; return path starves at ~0.01 Mbit/s | T01-topology-preconditions announced-address check | before any measurement |
| Three wrong return-relay stories from reading `resolved return path` | T08-relay-attribution line rule | any run |
| Downgrade cell crash-loops on a rewritten `.hopr-id` | harness (identity snapshot) | before any cell |
| Underlay pings to the connected client always time out (kill switch); all 64 episodes mis-attributed | T06-realtime-udp attribution rule | first attribution report |
| The delivered slow-relay mix (6 %) is not the minted mix (41 %); SURB spend is bursty | T09-impairment-ladder per-packet histogram | one rung |
| Upload +50–82 % from the client host's TCP stack alone; download null | T32-congestion-control | one ABBA series |
| The client's own health checks of other destinations ride the mixnet every 15 s | T16-metric-sampling covariate | one paired run |
| A 5 s stall and a uniformly slow transfer report the same Mbit/s | T04-fixed-throughput per-second sampling | any run |
| Client 0.96.1 never promotes its session target; exit env knob as workaround | T25-knob-ab knob list | one A/B |
| A stressed client balancer that does not limit throughput (r = 0.19) | T16-metric-sampling correlation rule | one download |
| Four Contabo probes reprovisioned unnoticed; keys and passwords changed | harness (fleet preflight) | before any cell |
