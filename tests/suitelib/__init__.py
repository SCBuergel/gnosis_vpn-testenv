"""Regression-suite library (docs/regression-catalogue.md).

config   knobs: environment, command line, --fast / --very-fast, per-test T<NN>_<VAR> overrides
verdicts the run directory (rows.jsonl, verdicts.jsonl, logs/, samples/) and per-test verdicts
client   the gnosis_vpn client: ctl, connect/disconnect with an armed deadman, log error counters, telemetry
cluster  the hoprd localcluster: status JSON, node REST and /metrics, live tc netem, the node sampler
target   the in-cluster traffic target: sized HTTP transfers, per-second interface sampling, UDP probes
stats    small numeric helpers
machineclass  relay machine classes per provider and size, and their thresholds (T36-relay-machine-class)
tomlcfg  line-based TOML section editing for client-config cells
"""
