# Testing framework for the regression suite

Requested in the review of the first suite revision, which was bash scripts on a shared `lib.sh`. The question: which framework fits a suite that drives a live stack (a hoprd localcluster, an exit server, one or more client containers, a traffic target) through `docker exec`, REST and `/metrics`, runs for minutes to an hour per test, must record many measurements per test, and has to run unchanged against a production network from a laptop.

## What the suite needs from a framework

| Need | Why |
| --- | --- |
| Fixtures with setup/teardown that survive a failing test | a client left connected strands a host behind its kill switch; the deadman and the disconnect-on-exit are not optional |
| Parameters overridable from the command line and the environment, per test | the same test runs at 15 s (`--very-fast`), 90 s (`--fast`) and 300 s, and against a localcluster or a production exit |
| Several verdicts per test, machine-readable, plus free-form measurements | a run is diffed as `rows.jsonl`/`verdicts.jsonl`, not read as a log; a gate records five checks and a diagnostic records numbers with no pass/fail |
| Deterministic order and a hard stop on a failed precondition | T03 must run before any gate, T05/T06 before an hour of load; a broken T01 makes every later number meaningless |
| Timeouts on every test and every subprocess | a hung `docker exec` or a probe that never reports must not hang a nightly run |
| Standard result formats for CI and dashboards | JUnit XML is what every CI reads; a custom format needs a custom reader |
| The language the probes and target already use | the UDP probes (`probes/`) and the target services (`docker/target/`) are Python; one language means one set of shared code and one self-test run |
| No build step, few dependencies | the suite runs on a bare Ubuntu host next to the stack; a toolchain per run is a cost paid on every host |

## Candidates

| Framework | Fit | Verdict |
| --- | --- | --- |
| **bash + bats** (first revision) | bats covers unit checks of shell functions; the live tests were plain scripts with a hand-rolled verdict/row library, subshell tricks for parallel clients, and no timeouts. Two harness bugs (the deadman disarm race, the deadman firing inside long sessions) came from exactly that hand-rolled plumbing and cost a week of wrong findings. | replaced |
| **pytest** | fixtures with scope and teardown, `conftest.py` for options and hooks, markers for kinds, `-k`/custom `--only` for selection, JUnit XML built in, `pytest.skip` for missing stack features, plain Python for the verdict recorder and for sharing code with the probes and the target. hoprnet already ships its Python tooling as packages (`sdk/python/api`, `sdk/python/localcluster`), so Python is a language the team maintains. Stdlib only apart from pytest itself. | chosen |
| **Robot Framework** | keyword tables read well for manual QA but every keyword is Python underneath; the measurement logic would live in a Python library anyway and the table layer adds a second language and a second toolchain. | no |
| **Rust integration tests (`cargo test`)** | the same language as hoprd and the client; but a test binary that drives docker, tc and REST is a small application, compile times land on the measurement host (forbidden while a run is live), and the probes would have to move to Rust or stay Python across a boundary. | no |
| **Go `testscript` / testing** | good process control and timeouts, but a new language for the team's test tooling and no reuse of the Python probes. | no |
| **Ansible / a CI YAML pipeline as the runner** | good at orchestration across hosts, poor at expressing per-arm verdicts and at running the same test locally; the suite still needs a test framework underneath. | as an outer layer only |

## What pytest gives the suite, concretely

- `conftest.py` owns the mechanics that used to be `run.sh` and `lib.sh`: the run directory and its refusal of a reused id, `--fast`/`--very-fast`/`--knob`, `--only`/`--skip`, T01 aborting the run, per-test timeouts (`TIMEOUT(knobs)`), the console log, the summary and `junit.xml`.
- Fixtures: `cfg`, `run`, `client`/`clients`/`client2`, `cluster`/`live_cluster`, `target`, `checks`, `knobs`. A session is a context manager (`with client.connect(dest, idle) as s:`); an autouse fixture disconnects whatever a failed test left connected.
- A test module declares `TEST`, `KIND` and `KNOBS`; every knob is overridable as `--knob T<NN>_<VAR>=…` or `T<NN>_<VAR>` in the environment, so load, size, rate, host and duration can be changed per invocation without editing a test.
- Production networks: `--client`, `--dest`, `--target HOST` and `--no-cluster` run the same tests against any client container and any host running the target services (`docker/target`, plain Python modules); cluster-only checks skip and say so.
- Offline self-tests (`just suite-selftest`, 25 cases) cover the library and run every probe against every target service on loopback, so the wire protocol is checked without a stack.

## What it does not solve

- A test that needs root (`tc netem`, SIGSTOP of a relay) still needs root; pytest only makes the skip explicit.
- Long runs still need a detached launcher on the host (`systemd-run … -p KillMode=process`); pytest is the process being detached, not the detacher.
- The measurement discipline in `AGENTS.md` (do not idle before measuring, a measurement needs a sample, arms do not share sessions) is not a framework property. A fixture makes it easy to follow, not impossible to break.
