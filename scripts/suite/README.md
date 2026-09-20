# Regression suite

Scripted form of [docs/regression-catalogue.md](../../docs/regression-catalogue.md): one `tNN.sh` per catalogue test, a shared `lib.sh`, the runner `run.sh` and a version-matrix driver (`matrix.sh`). Everything runs against the live testenv stack through `docker exec gnosis_vpn-client gnosis_vpn-ctl …`, the localcluster's `status` JSON and the nodes' REST/`/metrics` endpoints; traffic goes to the in-cluster target container (`just target-start`).

```sh
just up-nobuild                  # or: just up   — stack + target + clients
just suite                       # THE run: every test, one order (t01 … t24); T03 writes the band on the way
just suite --fast                # the catalogue's shorter durations; --very-fast is more aggressive still
just suite --only t09,t22        # one or a few tests; --skip tNN drops one; T22_LADDER="1 2" is a per-test knob
just test t04                    # one test, outside the runner
just matrix scripts/suite/cells/example.cells all --fast --ref-cell A-0961-hoprd412
```

There is one run and no profiles: `run.sh` executes t01 … t24 in a fixed order every time, and a run id (`--run-id`) that already holds results is refused rather than appended to. T05-loaded-latency sits fifth and T06-realtime-udp sixth, right after the T04-fixed-throughput reference, because their numbers are only comparable on a host that has not been loaded for an hour first.

**Three kinds, scored differently.** Each `tNN.sh` declares `suite_kind gate|diagnostic|runbook`. Only a **gate** can fail a run; a **diagnostic** emits `RECORDED` and never scores; **runbook** items (fleet, investigation, tooling: t25 t26 t27 t28 t29 t30 t31 t32) are not in the run and are started explicitly with `just test tNN`. A `FAIL` raised by a non-gate is downgraded to `WARN`, so tooling can never pad or redden a summary.

**Relative scoring.** Throughput and latency are host artifacts on a single-host stack, so `score_delta` scores them against the last stored value of the same metric on the same stack (normally the previous run), using the tolerance from T03-repeatability-baseline's band; `--ref-cell` is only for the explicit A/B path of `matrix.sh`. Absolute pass/fail is reserved for discrete assertions. Without a T03-repeatability-baseline record for the stack the suite runs and records everything but refuses to score those numbers, and says so in the run header.

**Expected failures** use `xfail TEST FIXED_BY HOLDS msg`: a known defect with no fix in the tested stack is `XFAIL` tagged with the fix (T13-mtu-sweep → `hoprnet#8392`), and a surprise pass is `XPASS`, never swallowed.

Results: `SUITE_OUT_DIR/<run-id>/` with `rows.jsonl` (every measurement), `verdicts.jsonl` + `summary.csv` (one line per check), `provenance.json` (T02-build-provenance), `logs/` (client log slices), `samples/` (node/CPU samples), `persec-*.csv` (per-second interface bytes), probe JSON. `matrix.sh` adds `matrix-<stamp>.csv` across cells.

Conventions (same as `scripts/`): `set -euo pipefail`, `LC_ALL=C`, header comment, `--help`, every knob an env var with a default, `PASS`/`WARN`/`FAIL`/`SKIP` per check, non-zero exit on `FAIL`, machine-readable output next to the human output. Tests that need a stack feature the localcluster cannot provide `SKIP` with the reason (T27-role-split per-role versions, T28-transport-ab HTTP/3 target, T31-frame-forensics instrumented client image).

Every test that connects the tunnel arms a **deadman disconnect** (`DEADMAN`, default 900 s) and disarms it on exit, because the kill switch would otherwise strand a real host. A test whose session must outlive the default calls `deadman_cover DUR` before `connect` (T23-sustained-soak, T24-sustained-upload); without it the deadman disconnects the client mid-session and the probe reads the rest as loss. Saved client logs (`logs/`) drop the DEBUG path-planner lines unless `SAVE_LOG_RAW=1`; they were 90 % of the volume and filled the host's disk. Offline unit tests for the library: `bats scripts/tests/suite-lib.bats`.
