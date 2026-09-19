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

@test "score_delta records instead of scoring when no band exists" {
  # SUITE_REFS_DIR must be isolated in EVERY test that calls score_delta: it appends to the metric history, so a
  # test without this line writes its fixture value into the live history and the next real run scores against
  # it. That happened on 2026-09-17 (a T04-fixed-throughput FAIL against "stack deadbeef").
  export SUITE_BANDS_DIR="$SUITE_RUN/bands-empty" SUITE_REFS_DIR="$SUITE_RUN/refs-empty"
  SUITE_STACK_KEY=deadbeef score_delta T99 down_mbit 5
  run tail -1 "$SUITE_RUN/verdicts.jsonl"
  [[ "$output" == *'"status": "RECORDED"'* ]]
  [[ "$output" == *"no T03-repeatability-baseline"* ]]
}

@test "score_delta scores against the previous stored run, not a sibling cell" {
  export SUITE_BANDS_DIR="$SUITE_RUN/bands" SUITE_REFS_DIR="$SUITE_RUN/refs" SUITE_STACK_KEY=deadbeef
  mkdir -p "$SUITE_BANDS_DIR"
  echo '{"mde_pct":10}' > "$SUITE_BANDS_DIR/deadbeef.json"
  # first ever observation of this metric: stored as the baseline, not scored
  score_delta T99 m 10
  run tail -1 "$SUITE_RUN/verdicts.jsonl"
  [[ "$output" == *'"status": "RECORDED"'* ]]
  # -5 % against the stored 10, inside a 10 % band
  score_delta T99 m 9.5
  run tail -1 "$SUITE_RUN/verdicts.jsonl"
  [[ "$output" == *'"status": "PASS"'* ]]
  # -32 % against the previous run: a regression beyond the band
  score_delta T99 m 6.5
  run tail -1 "$SUITE_RUN/verdicts.jsonl"
  [[ "$output" == *'"status": "FAIL"'* ]]
  [[ "$output" == *"previous run"* ]]
}

@test "score_delta keeps an append-only history per metric" {
  export SUITE_BANDS_DIR="$SUITE_RUN/bands" SUITE_REFS_DIR="$SUITE_RUN/refs" SUITE_STACK_KEY=deadbeef
  mkdir -p "$SUITE_BANDS_DIR"; echo '{"mde_pct":10}' > "$SUITE_BANDS_DIR/deadbeef.json"
  score_delta T99 hm 1; score_delta T99 hm 2; score_delta T99 hm 3
  run wc -l < "$SUITE_RUN/refs/history/hm.jsonl"
  [ "$output" -eq 3 ]
}

@test "score_delta clamps an absurd band instead of passing every regression" {
  # The 2026-09-17 old-version run measured its band on the broken stack under test: T03-repeatability-baseline reported +-279 %, and a
  # delivery collapse from 99.7 % to 17.3 % scored PASS because nothing can exceed a 279 % tolerance. A band that
  # wide is evidence the stack is unstable, not licence to ignore regressions.
  export SUITE_BANDS_DIR="$SUITE_RUN/bands-wide" SUITE_REFS_DIR="$SUITE_RUN/refs-wide" SUITE_STACK_KEY=deadbeef
  export BAND_MAX_PCT=50
  mkdir -p "$SUITE_BANDS_DIR"; echo '{"mde_pct":279}' > "$SUITE_BANDS_DIR/deadbeef.json"
  score_delta T99 dp 99.7                      # baseline
  score_delta T99 dp 17.3                      # -82.7 %: catastrophic
  run tail -1 "$SUITE_RUN/verdicts.jsonl"
  [[ "$output" == *'"status": "FAIL"'* ]]
  [[ "$output" == *"band clamped"* ]]
  [[ "$output" == *"279"* ]]
}

@test "score_delta leaves a sane band alone" {
  export SUITE_BANDS_DIR="$SUITE_RUN/bands-ok" SUITE_REFS_DIR="$SUITE_RUN/refs-ok" SUITE_STACK_KEY=deadbeef
  export BAND_MAX_PCT=50
  mkdir -p "$SUITE_BANDS_DIR"; echo '{"mde_pct":18}' > "$SUITE_BANDS_DIR/deadbeef.json"
  score_delta T99 sane 10
  score_delta T99 sane 9.5                     # -5 %, inside 18 %
  run tail -1 "$SUITE_RUN/verdicts.jsonl"
  [[ "$output" == *'"status": "PASS"'* ]]
  [[ "$output" != *"band clamped"* ]]
}
