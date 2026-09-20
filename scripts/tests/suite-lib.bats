#!/usr/bin/env bats
# Offline checks for scripts/suite/lib.sh helpers (no docker, no network).

setup() {
  export SUITE_RUN="$(mktemp -d)"
  export LOCALCLUSTER_BIN=/nonexistent
  source "${BATS_TEST_DIRNAME}/../suite/lib.sh"
  suite_init
}

teardown() { rm -rf "$SUITE_RUN"; }

@test "stats_json computes median and stdev" {
  run stats_json 1 2 3 4 5
  [ "$status" -eq 0 ]
  [[ "$output" == *'"median": 3'* ]]
  [[ "$output" == *'"n": 5'* ]]
}

@test "stats_json ignores NA values" {
  run stats_json 4 NA 6
  [[ "$output" == *'"n": 2'* ]]
}

@test "emit_row writes one JSON line with parsed values" {
  emit_row T99 a=1 b=text 'c={"x":2}'
  run tail -1 "$SUITE_RUN/rows.jsonl"
  [[ "$output" == *'"test": "T99"'* ]]
  [[ "$output" == *'"a": 1'* ]]
  [[ "$output" == *'"b": "text"'* ]]
  [[ "$output" == *'"x": 2'* ]]
}

@test "verdict FAIL sets SUITE_FAILED and records the line" {
  verdict T99 FAIL "broken"
  [ "$SUITE_FAILED" -eq 1 ]
  run tail -1 "$SUITE_RUN/verdicts.jsonl"
  [[ "$output" == *'"status": "FAIL"'* ]]
}

@test "json_get walks dotted paths" {
  run json_get '{"a":{"b":[10,20]}}' a.b.1
  [ "$output" = "20" ]
}

@test "persec_stall finds the longest zero-progress run" {
  printf '1,100,0\n2,100,0\n3,100,0\n4,200,0\n5,200,0\n' > "$SUITE_RUN/persec-x.csv"
  run persec_stall x rx
  [ "$output" = "2" ]
}

@test "q picks the fast value when SUITE_FAST=1" {
  SUITE_FAST=0; [ "$(q 300 60)" = 300 ]
  SUITE_FAST=1; [ "$(q 300 60)" = 60 ]
}

@test "toml-edit set-section replaces and appends" {
  f="$SUITE_RUN/c.toml"; printf 'version = 6\n\n[a]\nx = 1\n\n[b]\ny = 2\n' > "$f"
  python3 "${BATS_TEST_DIRNAME}/../suite/toml-edit.py" "$f" set-section '[a]' 'x = 9'
  python3 "${BATS_TEST_DIRNAME}/../suite/toml-edit.py" "$f" set-section '[c]' 'z = 3'
  grep -q 'x = 9' "$f"; grep -q '\[c\]' "$f"; grep -q 'y = 2' "$f"
}

@test "toml-edit keep-destinations trims blocks" {
  f="$SUITE_RUN/d.toml"; printf '[destinations.a]\naddress = "1"\n\n[destinations.b]\naddress = "2"\n\n[connection]\nk = 1\n' > "$f"
  python3 "${BATS_TEST_DIRNAME}/../suite/toml-edit.py" "$f" keep-destinations 1
  grep -q 'destinations.a' "$f"; ! grep -q 'destinations.b' "$f"; grep -q '\[connection\]' "$f"
}

@test "record never sets SUITE_FAILED" {
  record T99 "a measurement"
  [ "$SUITE_FAILED" -eq 0 ]
  run tail -1 "$SUITE_RUN/verdicts.jsonl"
  [[ "$output" == *'"status": "RECORDED"'* ]]
}

@test "a FAIL from a non-gate is downgraded to WARN" {
  suite_kind diagnostic
  verdict T99 FAIL "not a gate"
  [ "$SUITE_FAILED" -eq 0 ]
  run tail -1 "$SUITE_RUN/verdicts.jsonl"
  [[ "$output" == *'"status": "WARN"'* ]]
  [[ "$output" == *'"kind": "diagnostic"'* ]]
}

@test "a FAIL from a gate fails the run" {
  suite_kind gate
  verdict T99 FAIL "gate broke"
  [ "$SUITE_FAILED" -eq 1 ]
}

@test "xfail records XFAIL when the known defect holds and XPASS when it does not" {
  xfail T99 "hoprnet#8392" 1 "defect reproduced"
  run tail -1 "$SUITE_RUN/verdicts.jsonl"
  [[ "$output" == *'"status": "XFAIL"'* ]]
  [[ "$output" == *'hoprnet#8392'* ]]
  xfail T99 "hoprnet#8392" 0 "defect absent"
  run tail -1 "$SUITE_RUN/verdicts.jsonl"
  [[ "$output" == *'"status": "XPASS"'* ]]
}
