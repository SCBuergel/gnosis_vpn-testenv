# Regression suite

pytest form of [docs/regression-catalogue.md](../../docs/regression-catalogue.md): one `tests/test_tNN_<name>.py` per catalogue test, the library `suitelib/`, the pytest glue `conftest.py`, and a version-matrix driver (`matrix.py`). Everything runs against the live testenv stack through `docker exec gnosis_vpn-client gnosis_vpn-ctl …`, the localcluster's `status` JSON and the nodes' REST/`/metrics` endpoints; traffic goes to the in-cluster target container (`just target-start`). Needs `pytest` (`apt install python3-pytest` or `pip install pytest`); everything else is the standard library.

```sh
just up-nobuild                  # or: just up   — stack + target + clients
just suite                       # THE run: every test, one order (t01 … t24); T03-repeatability-baseline records repeatability on the way
just suite --fast                # the catalogue's shorter durations; --very-fast is more aggressive still
just suite --only t09,t22        # one or a few tests (t01 still runs first); --skip tNN drops one
just suite --knob T22_LADDER="1 2"   # a per-test knob (or T22_LADDER="1 2" in the environment)
just test t04                    # one test, without t01 in front; runbook items (t25 … t32) run this way too
just suite --runbook             # include the runbook items in a run
just matrix scripts/suite/cells/example.cells --fast
just suite-selftest              # offline unit tests of suitelib, no stack
just suite --client NAME --dest ID --target HOST --no-cluster   # production network: any client container, any host running docker/target's services
```

There is one run and no profiles: `pytest` collects `tests/` in file order, t01 … t24, every time, and a run id (`--run-id`) that already holds results is refused rather than appended to. T05-loaded-latency sits fifth and T06-realtime-udp sixth, right after the T04-fixed-throughput reference, because their numbers are only comparable on a host that has not been loaded for an hour first. A T01-topology-preconditions failure aborts the run (every later test is skipped).

**Layout.** A test module declares `TEST` (its catalogue id, used as the verdict and row label), `KIND` (`gate`, `diagnostic` or `runbook`) and `KNOBS` (every knob with its default; `q(normal, fast)` gives a `--fast` value). Its one test function takes fixtures: `cfg` (global knobs), `run` (the results directory), `client` / `clients` / `client2` (client containers), `cluster` (localcluster status, node REST and metrics, live `tc netem`, the node sampler), `target` (the traffic target; the test skips when it is not running), `checks` (the verdict recorder) and `knobs` (the module's `KNOBS` resolved for this run). `client.connect(dest, idle)` returns a session that disconnects on leaving its `with` block; `client.probe(...)` runs one of `probes/` inside the container.

**Three kinds, scored differently.** Only a **gate** can fail a run; a **diagnostic** emits `RECORDED` and never scores (a `FAIL` it raises is downgraded to `WARN`); **runbook** items are not collected unless named. `checks.conclude()` (called by the harness after every test) turns a gate's recorded failures into the pytest failure, so a test can record several checks and the run's summary counts each one.

**Absolute thresholds.** Every number a gate holds a measurement against is a named knob with a default calibrated on the reference stack (`DOWN_MIN_MBIT=7`, `LOSS_MAX=5`, `RECOVER_MAX=90`, …), listed with its calibration in the catalogue's threshold table; nothing is compared with a previous run. `checks.assert_min` / `assert_max` print the knob next to the value. T03-repeatability-baseline records how far the stack's own numbers wander so the headroom can be judged.

**Knobs.** Every knob is an environment variable with a default. A per-test knob is `T<NN>_<VAR>` (`T23_DUR=150`), from the environment or `--knob`; it beats `--very-fast`, which beats the `--fast`/normal default. The global `BYTES`, `CAP`, `REPS` work the same way.

**Expected failures** use `checks.xfail(fixed_by, holds, msg)`: a known defect with no fix in the tested stack is `XFAIL` tagged with the fix, and a surprise pass is `XPASS`, never swallowed. No test carries one at the moment.

Results: `SUITE_OUT_DIR/<run-id>/` with `rows.jsonl` (every measurement), `verdicts.jsonl` + `summary.csv` (one line per check), `run.txt` (timings), `console.log`, `provenance.json` (T02-build-provenance), `logs/` (client log slices), `samples/` (node/CPU samples), `persec-*.csv` (per-second interface bytes), probe JSON. `matrix.py` adds `matrix-<stamp>.csv` across cells; `report.py RUN_DIR…` renders verdicts as a Markdown table.

Every test that connects the tunnel arms a **deadman disconnect** (`DEADMAN`, default 900 s) and disarms it on disconnect, because the kill switch would otherwise strand a real host. A test whose session must outlive the default calls `client.deadman_cover(DUR)` before `connect` (T23-sustained-soak, T24-sustained-upload); without it the deadman disconnects the client mid-session and the probe reads the rest as loss. Whatever a test does, the harness disconnects every client it left connected. Saved client logs (`logs/`) drop the DEBUG path-planner lines unless `SAVE_LOG_RAW=1`; they were 90 % of the volume and filled the host's disk.
