# Regression suite

Scripted form of [docs/regression-catalogue.md](../../docs/regression-catalogue.md): one `tNN.sh` per catalogue test, a shared `lib.sh`, a profile runner (`run.sh`) and a version-matrix driver (`matrix.sh`). Everything runs against the live testenv stack through `docker exec gnosis_vpn-client gnosis_vpn-ctl …`, the localcluster's `status` JSON and the nodes' REST/`/metrics` endpoints; traffic goes to the in-cluster target container (`just target-start`).

```sh
just up-nobuild                  # or: just up   — stack + target + client
just suite baseline              # ONCE PER STACK: writes the T03-repeatability-baseline band the gates are scored against
just test t04                    # one test
just suite gates                 # baseline | smoke | gates | regression | deep | soak | all  (--fast shortens)
just matrix scripts/suite/cells/example.cells regression --fast --ref-cell A-0961-hoprd412
```

**Three kinds, scored differently.** Each `tNN.sh` declares `suite_kind gate|diagnostic|runbook`. Only a **gate** can fail a run; a **diagnostic** emits `RECORDED` and never scores; **runbook** items (fleet, investigation, tooling: t25 t26 t27 t22 t28 t29 t30 t31 t32) are in no profile and are run explicitly. A `FAIL` raised by a non-gate is downgraded to `WARN`, so tooling can never pad or redden a summary.

**Relative scoring.** Throughput and latency are host artifacts on a single-host stack, so they are scored with `score_delta` against a reference cell in the same run (`--ref-cell`), using the tolerance from T03-repeatability-baseline's band. Absolute pass/fail is reserved for discrete assertions. Without a T03-repeatability-baseline record for the stack the suite runs and records everything but refuses to score those numbers, and says so in the run header.

**Expected failures** use `xfail TEST FIXED_BY HOLDS msg`: a known defect with no fix in the tested stack is `XFAIL` tagged with the fix (T13-mtu-sweep → `hoprnet#8392`), and a surprise pass is `XPASS`, never swallowed.

Results: `SUITE_OUT_DIR/<run-id>/` with `rows.jsonl` (every measurement), `verdicts.jsonl` + `summary.csv` (one line per check), `provenance.json` (T02-build-provenance), `logs/` (client log slices), `samples/` (node/CPU samples), `persec-*.csv` (per-second interface bytes), probe JSON. `matrix.sh` adds `matrix-<stamp>.csv` across cells.

Conventions (same as `scripts/`): `set -euo pipefail`, `LC_ALL=C`, header comment, `--help`, every knob an env var with a default, `PASS`/`WARN`/`FAIL`/`SKIP` per check, non-zero exit on `FAIL`, machine-readable output next to the human output. Tests that need a stack feature the localcluster cannot provide `SKIP` with the reason (T27-role-split per-role versions, T28-transport-ab HTTP/3 target, T31-frame-forensics instrumented client image).

Every test that connects the tunnel arms a **deadman disconnect** (`DEADMAN`, default 900 s) and disarms it on exit — the kill switch would otherwise strand a real host. Offline unit tests for the library: `bats scripts/tests/suite-lib.bats`.
