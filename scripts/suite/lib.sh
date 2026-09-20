#!/usr/bin/env bash
#
# lib.sh - shared helpers for the regression suite (docs/regression-catalogue.md).
#
# Sourced by every scripts/suite/t*.sh. Provides: client control through
# `docker exec <client> gnosis_vpn-ctl`, connect/disconnect with an armed deadman
# disconnect, client-log error counters, client telemetry counters, node REST/metrics
# access via the localcluster status JSON, sized HTTP transfers against the in-cluster
# target, per-second interface sampling inside the client, JSONL rows and
# PASS/WARN/FAIL/SKIP verdicts.
#
# Every knob is an env var with a default; tests add their own on top.

set -euo pipefail
export LC_ALL=C

SUITE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TESTENV_DIR="${TESTENV_DIR:-$(cd "${SUITE_LIB_DIR}/../.." && pwd)}"

# --fast on any test's command line selects the shortened durations (same as SUITE_FAST=1)
for _a in "$@"; do [ "${_a}" = "--fast" ] && export SUITE_FAST=1; done
: "${SUITE_FAST:=0}"
# q DEFAULT FAST — pick the value for the current mode (--fast selects the second)
q() { if [ "${SUITE_FAST}" = "1" ]; then echo "$2"; else echo "$1"; fi; }

: "${CLIENT:=gnosis_vpn-client}"          # container of the active client
: "${CLIENT2:=gnosis_vpn-client-2}"       # second client (T19-background-load/T21-passive-observer), optional
: "${CLIENT_COUNT:=1}"                    # client containers started by `just clients-start` (T22-concurrent-clients)
: "${SURB_RAMP_WAIT:=25}"                 # seconds after connect before measuring; see the note on connect()
# Widest usable scoring tolerance. T03-repeatability-baseline measures the band on the stack under test, so a BROKEN stack measures a
# huge spread and buys itself a tolerance nothing can fail against: on the 2026-09-17 old-version run the band
# came out at +-279 % and a delivery collapse from 99.7 % to 17.3 % scored PASS. A band wider than this is
# evidence the stack is unstable, not licence to ignore regressions, so scoring clamps to it and says so.
: "${BAND_MAX_PCT:=50}"
: "${DEST:=node-0}"                       # destination id in client.toml (exit-adjacent node)
: "${TARGET_NAME:=gnosis_vpn-target}"     # in-cluster traffic target container
: "${DOCKER_NETWORK:=gnosis-vpn-testenv}"
: "${DATA_DIR:=/tmp/hopr-nodes}"
: "${CONFIG_DIR:=/tmp/gnosis_vpn-testenv}"
: "${CLUSTER_SIZE:=3}"
: "${LOCALCLUSTER_BIN:=${HOPRD_DIR:-${TESTENV_DIR}/../hoprd}/result-localcluster/bin/hoprd-localcluster}"
: "${SUITE_OUT_DIR:=/tmp/gnosis_vpn-testenv-suite}"
: "${SUITE_RUN:=${SUITE_OUT_DIR}/latest}"
: "${SUITE_CELL:=}"                       # label of the version/config cell, set by matrix.sh
: "${SUITE_REF_CELL:=}"                   # only used by the explicit A/B path (matrix.sh); normally empty
: "${SUITE_RUN_GROUP:=default}"           # only used by the explicit A/B path
: "${SUITE_BANDS_DIR:=${SUITE_OUT_DIR}/bands}"   # T03-repeatability-baseline bands, keyed on stack provenance
: "${SUITE_REFS_DIR:=${SUITE_OUT_DIR}/refs}"     # per-metric reference values within a run group
# SUITE_MODE=full|fast|veryfast (run.sh sets it; a test run on its own derives it from SUITE_FAST). Delta history is
# kept PER MODE: a --very-fast T13 read 8.6 Mbit/s from a 2 MB transfer against 12.3 from the full run's 10 MB and
# FAILed by -30 % (vfreview2, 2026-09-20), and the smoke number would then have become the next full run's reference.
: "${SUITE_MODE:=$([ "${SUITE_FAST}" = 1 ] && echo fast || echo full)}"
: "${DEADMAN:=900}"                       # armed disconnect fires after this many seconds
: "${CONNECT_TIMEOUT:=240}"
# transfer size and cap: a single-host localcluster moves a few Mbit/s, so 10 MB / 90 s completes where the
# catalogue's field default (25 MB / 60 s against a public exit) would only measure the cap
: "${BYTES:=$(q 10000000 5000000)}"
: "${CAP:=$(q 90 60)}"
: "${REPS:=$(q 3 2)}"
: "${WG_IFACE:=}"                         # auto-detected inside the client when empty

SUITE_FAILED=0
DEADMAN_PID=""
DEADMAN_PIDFILE=""

# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
suite_log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
now_ms() { date +%s%3N; }
utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

suite_init() {
  mkdir -p "${SUITE_RUN}"
  SUITE_RUN=$(readlink -f "${SUITE_RUN}")           # resolve the 'latest' symlink: the container sees the real dir
  SUITE_RUN_IN_CLIENT="/suite-out/$(basename "${SUITE_RUN}")"   # SUITE_OUT_DIR is mounted at /suite-out in the client
  mkdir -p "${SUITE_RUN}/logs" "${SUITE_RUN}/samples"
  : >> "${SUITE_RUN}/rows.jsonl"
  : >> "${SUITE_RUN}/verdicts.jsonl"
}

# emit_row TEST key=value ... — values parse as JSON when they can, else as strings
emit_row() {
  python3 - "${SUITE_RUN}/rows.jsonl" "${SUITE_CELL}" "$@" <<'PY'
import sys, json, time
out, cell, test = sys.argv[1], sys.argv[2], sys.argv[3]
row = {"t": round(time.time(), 3), "cell": cell, "test": test}
for kv in sys.argv[4:]:
    k, _, v = kv.partition("=")
    try:
        row[k] = json.loads(v)
    except Exception:
        row[k] = v
with open(out, "a") as f:
    f.write(json.dumps(row) + "\n")
PY
}

# ---------------------------------------------------------------------------
# test kinds and scoring
# ---------------------------------------------------------------------------
# Every test declares one kind. Only a *gate* can turn a run red:
#   gate        a regression gate: it must be able to pass on a healthy stack
#   diagnostic  measured and recorded, never scored (characterizations, controls, covariates)
#   runbook     fleet / investigation / tooling: kept in the repo, excluded from profiles
# Host-dependent numbers (throughput, latency) are never absolute gates on a single-host stack:
# they score as a delta against a named reference cell in the same run, inside T03-repeatability-baseline's band.
# Absolute pass/fail is reserved for discrete assertions (zero decap errors, channel membership,
# the shaper present or absent, one datagram per read).
: "${SUITE_TEST_KIND:=gate}"
suite_kind() { SUITE_TEST_KIND="$1"; }

_verdict_write() {   # _verdict_write TEST STATUS MSG
  python3 - "${SUITE_RUN}/verdicts.jsonl" "${SUITE_CELL}" "$1" "$2" "$3" "${SUITE_TEST_KIND}" <<'VW'
import sys, json, time
out, cell, t, st, msg, kind = sys.argv[1:7]
with open(out, "a") as f:
    f.write(json.dumps({"t": round(time.time(), 1), "cell": cell, "test": t,
                        "status": st, "kind": kind, "msg": msg}) + "\n")
VW
}

# verdict TEST STATUS message — PASS/FAIL/WARN/SKIP. A FAIL from a non-gate is recorded as WARN:
# only gates may fail a run.
verdict() {
  local t="$1" st="$2"; shift 2
  if [ "${st}" = "FAIL" ] && [ "${SUITE_TEST_KIND}" != "gate" ]; then st="WARN"; fi
  printf '%s %s: %s\n' "${st}" "${t}" "$*"
  _verdict_write "${t}" "${st}" "$*"
  [ "${st}" = "FAIL" ] && SUITE_FAILED=1 || true
}

# record TEST message — a measurement with no pass/fail. Never scores, never pads a summary.
record() {
  local t="$1"; shift
  printf 'RECORDED %s: %s\n' "${t}" "$*"
  _verdict_write "${t}" "RECORDED" "$*"
}

# xfail TEST FIXED_BY HOLDS message — a known defect with no fix in the tested stack.
# HOLDS=1: the known-broken behaviour was observed -> XFAIL (expected, not a failure).
# HOLDS=0: it did not occur -> XPASS, reported as WARN-level so a silent fix is never swallowed.
xfail() {
  local t="$1" fixed_by="$2" holds="$3"; shift 3
  if [ "${holds}" = "1" ]; then
    printf 'XFAIL %s: %s [expected until %s]\n' "${t}" "$*" "${fixed_by}"
    _verdict_write "${t}" "XFAIL" "$* [expected until ${fixed_by}]"
  else
    printf 'XPASS %s: %s [expected to fail until %s - confirm the fix landed, then make this a gate]\n' "${t}" "$*" "${fixed_by}"
    _verdict_write "${t}" "XPASS" "$* [expected to fail until ${fixed_by} - confirm the fix landed, then make this a gate]"
  fi
}

# ---------------------------------------------------------------------------
# stack identity, T03-repeatability-baseline bands, delta scoring
# ---------------------------------------------------------------------------
# stack_key — stable id of the software+config under test, so a T03-repeatability-baseline band belongs to one stack only
stack_key() {
  if [ -n "${SUITE_STACK_KEY:-}" ]; then echo "${SUITE_STACK_KEY}"; return; fi
  local c h s
  c=$(ctl --version 2>/dev/null | tr -d '\n' || echo unknown-client)
  h=$("${HOPRD_BIN:-hoprd}" --version 2>/dev/null | head -1 || echo unknown-hoprd)
  s=$(docker exec gnosis_vpn-server-0 ./gnosis_vpn-server --version 2>/dev/null | head -1 || echo unknown-server)
  SUITE_STACK_KEY=$(printf '%s|%s|%s|%s|%s|%s' "$c" "$h" "$s" "${CLUSTER_ENV:-}" "${CLIENT_EXTRA_ENV:-}" "${CLUSTER_SIZE:-}" \
    | sha256sum | cut -c1-12)
  export SUITE_STACK_KEY
  echo "${SUITE_STACK_KEY}"
}

band_file() { echo "${SUITE_BANDS_DIR}/$(stack_key).json"; }
have_band() { [ -s "$(band_file)" ]; }
band_get() { python3 -c 'import sys,json; print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$(band_file)" "$1" 2>/dev/null; }

# band_write JSON — T03-repeatability-baseline stores the repeatability band for this stack
band_write() {
  mkdir -p "${SUITE_BANDS_DIR}"
  python3 -c 'import sys,json,time
d=json.loads(sys.argv[2]); d["stack_key"]=sys.argv[3]; d["recorded"]=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
json.dump(d, open(sys.argv[1],"w"), indent=1)' "$(band_file)" "$1" "$(stack_key)"
  suite_log "T03-repeatability-baseline band written for stack $(stack_key): $1"
}

# score_delta TEST METRIC VALUE [higher_better|lower_better] — longitudinal scoring for host-dependent numbers.
# The suite runs one stack at a time (versions sequentially), so a number is judged against the LAST STORED
# value for that metric — the previous run, usually the previous version — not against a sibling cell.
# History is append-only in SUITE_REFS_DIR/history/<metric>.jsonl so drift stays visible.
# Without a T03-repeatability-baseline band for this stack there is no defensible tolerance, so the value is RECORDED, never scored.
score_delta() {
  local t="$1" m="$2" v="$3" dir="${4:-higher_better}"
  local suffix=""; [ "${SUITE_MODE}" = full ] || suffix="@${SUITE_MODE}"
  local hist="${SUITE_REFS_DIR}/history/${m}${suffix}.jsonl"
  mkdir -p "$(dirname "$hist")"
  local prev; prev=$(tail -1 "$hist" 2>/dev/null || true)
  # append this observation first so the history is complete even when it cannot be scored
  python3 -c 'import sys,json,time
json.dump({"t":round(time.time(),1),"stack_key":sys.argv[2],"cell":sys.argv[3],"metric":sys.argv[4],"value":float(sys.argv[5])},
          open(sys.argv[1],"a")); open(sys.argv[1],"a").write("\n")' \
    "$hist" "$(stack_key)" "${SUITE_CELL:-default}" "$m" "$v" 2>/dev/null || true
  if ! have_band; then record "$t" "${m}=${v} (unscored: no T03-repeatability-baseline for stack $(stack_key))"; return 0; fi
  if [ -z "$prev" ]; then record "$t" "${m}=${v} (first observation for this metric in ${SUITE_MODE} mode, stored as the baseline)"; return 0; fi
  local tol out raw_tol clamped=0; raw_tol=$(band_get mde_pct); raw_tol=${raw_tol:-20}; tol=$raw_tol
  if python3 -c "import sys; sys.exit(0 if float('$raw_tol') > float('$BAND_MAX_PCT') else 1)"; then
    tol=$BAND_MAX_PCT; clamped=1
  fi
  out=$(python3 - "$m" "$v" "$prev" "$tol" "$dir" "$(stack_key)" <<'SD'
import sys, json
m, v, prev, tol, d, key = sys.argv[1], float(sys.argv[2]), json.loads(sys.argv[3]), float(sys.argv[4]), sys.argv[5], sys.argv[6]
ref = prev["value"]
if ref == 0:
    print("RECORDED|%s=%s (previous value was zero)" % (m, v)); raise SystemExit
chg = (v - ref) / ref * 100.0
worse = chg < -tol if d == "higher_better" else chg > tol
same_stack = prev.get("stack_key") == key
against = "the previous run of this same stack" if same_stack else ("the previous run (stack %s)" % prev.get("stack_key"))
print("%s|%s=%.3f vs %.3f from %s (%+.1f%%, band +-%.0f%% from T03-repeatability-baseline)" % ("FAIL" if worse else "PASS", m, v, ref, against, chg, tol))
SD
)
  [ -z "$out" ] && { record "$t" "${m}=${v} (delta scoring failed)"; return 0; }
  local st="${out%%|*}" msg="${out#*|}"
  [ "$clamped" = 1 ] && msg="${msg} [band clamped: T03-repeatability-baseline measured +-${raw_tol}%, above BAND_MAX_PCT ${BAND_MAX_PCT}% - the baseline stack was unstable, so this was scored at the cap]"
  if [ "$st" = "RECORDED" ]; then record "$t" "$msg"; else verdict "$t" "$st" "$msg"; fi
}

# stats_json n1 n2 ... -> {"n":..,"min":..,"median":..,"mean":..,"max":..,"stdev":..}
stats_json() {
  python3 - "$@" <<'SJ'
import sys, json, statistics as st
v = [float(x) for x in sys.argv[1:] if x not in ("", "NA", "None", "null")]
if not v:
    print(json.dumps({"n": 0})); sys.exit()
print(json.dumps({"n": len(v), "min": min(v), "median": st.median(v), "mean": round(st.mean(v), 3),
                  "max": max(v), "stdev": round(st.pstdev(v), 3)}))
SJ
}

json_get() { # json_get '<json>' key.path
  python3 -c 'import sys,json
d=json.loads(sys.argv[1])
for k in sys.argv[2].split("."):
    if isinstance(d,list): d=d[int(k)]
    else: d=d.get(k) if isinstance(d,dict) else None
print("" if d is None else (json.dumps(d) if isinstance(d,(dict,list)) else d))' "$1" "$2"
}

# ---------------------------------------------------------------------------
# client control
# ---------------------------------------------------------------------------
ctl() { docker exec "${CLIENT}" gnosis_vpn-ctl "$@"; }
ctl_json() { docker exec "${CLIENT}" gnosis_vpn-ctl -o json "$@"; }
# client_name N — container name of the Nth client: 1 is the primary, N>1 are the extras started by
# `just clients-start` on extra identity N-1. Run lib helpers against another client by overriding CLIENT in a
# subshell: CLIENT="$(client_name 3)" bash -c "source lib.sh; connect ..."
client_name() { if [ "$1" = 1 ]; then echo "${CLIENT}"; else echo "${CLIENT}-$1"; fi; }
# clients_running — how many consecutive client containers are up, starting at 1
clients_running() {
  local i n=0
  for i in $(seq 1 16); do
    if docker container inspect "$(client_name "$i")" >/dev/null 2>&1; then n=$i; else break; fi
  done
  echo "$n"
}
in_client() { docker exec "${CLIENT}" sh -c "$*"; }
in_client_bg() { docker exec -d "${CLIENT}" sh -c "$*"; }

client_status_text() { ctl status 2>/dev/null || true; }
client_is_connected() { client_status_text | grep -q '^Connected to' ; }   # 0.96.x prints it on its own line, not the first
client_worker_online() { ! client_status_text | head -1 | grep -q 'Worker offline'; }
dest_health_line() { client_status_text | grep "^${1:-$DEST} Route health:" || true; }
dest_is_ready() { dest_health_line "${1:-$DEST}" | grep -q 'Ready to connect'; }

wait_worker() {
  local t=${1:-120} i=0
  until client_worker_online; do
    sleep 2; i=$((i+2)); [ "$i" -ge "$t" ] && { suite_log "worker still offline after ${t}s"; return 1; }
  done
}

wait_dest_ready() {
  local d=${1:-$DEST} t=${2:-$CONNECT_TIMEOUT} i=0
  until dest_is_ready "$d"; do
    sleep 3; i=$((i+3))
    if [ "$i" -ge "$t" ]; then suite_log "destination $d not Ready after ${t}s: $(dest_health_line "$d")"; return 1; fi
  done
}

# The deadman runs fully detached with its stdio closed: a plain "( sleep; … ) &" inherits the pipe to the
# runner's tee and holds it open for the whole timeout, so every connecting test would take DEADMAN seconds.
#
# `sleep && disconnect`, NOT `sleep; disconnect`: when disarm_deadman kills the sleep, bash must not fall through
# to the disconnect. It did, and because disarm killed the sleep before the bash, every connect() made in a
# subshell (CLIENT=… bash -c "source lib.sh; connect …", T19-background-load, T22-concurrent-clients) disconnected
# its own client about one second after returning. fullrun5 T22-concurrent-clients: 5 of 7 clients across the
# ladder moved 0 bytes; each log shows a Disconnect command ~30 s after Connect with the tunnel ping healthy, and
# 2 of 4 surviving at n=4 is the race between the two kills. disarm_deadman now kills the bash first.
arm_deadman() {
  disarm_deadman
  DEADMAN_PIDFILE="${SUITE_RUN}/.deadman-${CLIENT}.pid"
  # the bash records its own pid: setsid(1) forks when its caller is a process-group leader, and then $! is not it
  setsid bash -c "echo \$\$ > '${DEADMAN_PIDFILE}'; sleep ${DEADMAN} && docker exec ${CLIENT} gnosis_vpn-ctl disconnect >/dev/null 2>&1 || true" \
    >/dev/null 2>&1 </dev/null &
  DEADMAN_PID=$!
  disown "$DEADMAN_PID" 2>/dev/null || true
}
disarm_deadman() {
  local p kids
  for p in $(cat "${DEADMAN_PIDFILE:-/dev/null}" 2>/dev/null) ${DEADMAN_PID}; do
    kids=$(pgrep -P "$p" 2>/dev/null || true)
    kill "$p" 2>/dev/null || true          # the bash first, so it can never run the disconnect on its way out
    [ -n "$kids" ] && kill $kids 2>/dev/null || true
  done
  rm -f "${DEADMAN_PIDFILE:-}" 2>/dev/null || true
  DEADMAN_PID=""
}
# deadman_cover SECONDS — raise DEADMAN so the armed disconnect cannot fire inside a session that must last SECONDS
# (plus the ramp wait and a probe's end-of-stream report). Call it before connect(). T23-sustained-soak (3600 s)
# and T24-sustained-upload (900 s) ran under the 900 s default: the deadman disconnected the client at +15 min,
# the probe counted the rest as loss — fullrun5 T23 delivered 24 % = 900/3600 and still PASSed, because a deadman
# disconnect is not a reconnect — and T24's upload never received its server report, so loss printed blank.
deadman_cover() { DEADMAN=$(python3 -c "print(max(int('${DEADMAN}'), int('$1') + ${SURB_RAMP_WAIT} + 120))"); }
trap 'disarm_deadman' EXIT

# connect [DEST] [IDLE_SECONDS] — arms the deadman, connects, waits for Connected, idles.
# Sets CONNECT_MS (time to Connected), LOG_SINCE (docker --since stamp), WG_IFACE.
#
# The idle is floored at SURB_RAMP_WAIT. The client ramps its SURB target from the ping tier to the main tier,
# so a measurement taken inside that window reads a transient, not steady state. Ramp length is CLIENT-specific:
# gnosis_vpn 0.96.2 defaults to 20 s (connection/options.rs SurbRampOptions), our older fix-surb-ramp branch to
# 60 s. 25 s clears 0.96.2's ramp.
#
# DO NOT raise this to "safely past" a long ramp. A default of 75 s was tried on 2026-09-18 and was actively
# harmful: during a long post-connect idle the return-path SURBs expire ("evicting surb ... cause=Expired"), so
# the FIRST sustained transfer afterwards starves the return path, the client's tunnel-ping times out, and the
# watchdog reconnects — killing whichever transfer ran first and reading as a catastrophic result. It cost a
# full day of false "hoprd regression" findings. Measured: with a 75 s wait, 8/8 first-transfers failed on BOTH
# hoprd 4.1.2 and 60269a3; with a short wait, 6/6 passed on both. If a client needs a longer ramp, shorten the
# ramp (GNOSISVPN_SURB_RAMP_SECS / [connection.surb_balancing.ramp]) rather than idling longer.
#
# Tests that deliberately measure the ramp itself — T07-cold-start's cold arm, T15-warmup-knee's
# delay sweep — opt out with RAMP_WAIT_OPT_OUT=1, which is the ONLY correct reason to set it.
connect() {
  local d=${1:-$DEST} idle=${2:-0}
  wait_dest_ready "$d"
  LOG_SINCE=$(utc_now)
  arm_deadman
  local t0; t0=$(now_ms)
  ctl connect "$d" >/dev/null 2>&1 || true
  local i=0
  until client_is_connected; do
    sleep 1; i=$((i+1))
    if [ "$i" -ge "${CONNECT_TIMEOUT}" ]; then
      suite_log "connect to $d timed out after ${CONNECT_TIMEOUT}s: $(client_status_text | head -1)"
      ctl disconnect >/dev/null 2>&1 || true; disarm_deadman; return 1
    fi
  done
  CONNECT_MS=$(( $(now_ms) - t0 ))
  WG_IFACE=$(in_client 'ls /sys/class/net | grep -m1 "^wg" || true')
  if [ "${RAMP_WAIT_OPT_OUT:-0}" != 1 ] && [ "$idle" -lt "${SURB_RAMP_WAIT}" ]; then idle="${SURB_RAMP_WAIT}"; fi
  [ "$idle" -gt 0 ] && sleep "$idle"
  return 0
}

disconnect() {
  ctl disconnect >/dev/null 2>&1 || true
  local i=0
  while client_is_connected; do sleep 1; i=$((i+1)); [ "$i" -ge 60 ] && break; done
  disarm_deadman
}

# client_restart — stop/start the client container (config or image changes); waits for the worker
# client_restart — stop/start the client container (config or image changes); waits for the worker and for the
# primary destination to be Ready again (a fresh worker re-syncs and health-checks before it can connect)
client_restart() {
  if ! ( cd "${TESTENV_DIR}" && just client-stop >/dev/null 2>&1 && just client-start >/dev/null ); then
    suite_log "client restart failed"; return 1
  fi
  sleep 3
  wait_worker 180 || return 1
  if ! wait_dest_ready "$DEST" "${RESTART_READY_TIMEOUT:-600}"; then
    # keep the evidence: the container is recreated with --rm on the next restart and its log is gone with it
    local ts; ts=$(date -u +%H%M%S)
    { echo "== status"; client_status_text; echo "== config"; cat "${CONFIG_DIR}/client.toml"; echo "== log (last 12 min)"; docker logs --since 12m "${CLIENT}" 2>&1; } \
      > "${SUITE_RUN}/logs/restart-not-ready-${ts}.log" 2>&1 || true
    suite_log "warning: $DEST not Ready ${RESTART_READY_TIMEOUT:-600}s after client restart (evidence: logs/restart-not-ready-${ts}.log)"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# client log error counters and telemetry
# ---------------------------------------------------------------------------
# log_errors SINCE [CONTAINER] -> JSON with the four catalogue error classes + extras
log_errors() {
  docker logs --since "$1" "${2:-$CLIENT}" 2>&1 | python3 -c '
import sys, json, re
# only WARN/ERROR lines count; DEBUG chatter (routing_actor "should_reconnect", telemetry dumps that mention
# "reassembly" in metric help text) produced false positives
level = re.compile(r"\b(WARN|ERROR)\b")
# the periodic liveness ping only logs its result at DEBUG; it is counted regardless of level because three of
# them are what a "reconnect" is made of, and a reconnect with N ping timeouts before it is a liveness-target
# problem (see T01-topology-preconditions) until proven otherwise
pingfail = re.compile(r"TunnelPingResult: Error\(Ping timed out\)")
pats = {
  "frame_discarded":      re.compile(r"frame.*(discard|expired)|discard.*frame", re.I),
  "reassembly_failed":    re.compile(r"failed to reassemble", re.I),
  "decap_error":          re.compile(r"decapsulat", re.I),
  "tunnel_ping_exceeded": re.compile(r"tunnel ping exceeded", re.I),
  "decap_stalled":        re.compile(r"DecapStalled"),
  "reconnects":           re.compile(r"exceeded max failures - reconnecting|restarting connection worker|requesting reconnect", re.I),
  "ping_timeouts":        re.compile(r"TunnelPingResult: Error\(Ping timed out\)"),
  "no_surb":              re.compile(r"no surb|NoSurb|out of surb", re.I),
  "warn_error_lines":     re.compile(r"."),
}
c = {k: 0 for k in pats}
ansi = re.compile(r"\x1b\[[0-9;]*m")
for line in sys.stdin:
    line = ansi.sub("", line)
    if "received worker response" in line: continue
    if pingfail.search(line): c["ping_timeouts"] += 1; continue
    if not level.search(line):
        continue
    for k, p in pats.items():
        if p.search(line): c[k] += 1
print(json.dumps(c))'
}

# telemetry_metric NAME -> summed value of all series named NAME (labels ignored); empty if absent
telemetry_metric() {
  ctl_json telemetry 2>/dev/null | python3 -c '
import sys, json, re
name = sys.argv[1]
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    txt = d if isinstance(d, str) else json.dumps(d)
except Exception:
    txt = raw
txt = txt.replace("\\n", "\n")
tot = 0.0; n = 0
for line in txt.splitlines():
    line = line.strip().strip("\"")
    if line.startswith(name) and (line[len(name):len(name)+1] in ("{", " ")):
        try:
            tot += float(line.rsplit(" ", 1)[1]); n += 1
        except Exception:
            pass
print("" if n == 0 else (int(tot) if tot == int(tot) else tot))' "$1"
}

# save_client_log NAME SINCE [CONTAINER] — the client's log slice for the window. The DEBUG path-planner/selector
# lines (the target T08-relay-attribution parses live from docker logs, ~40 MB/min, about 90 % of the volume) are
# dropped unless SAVE_LOG_RAW=1: fullrun5 saved 30 GB of them and fullrun6 died at T10 with the disk full.
# WARN/ERROR lines from those targets are kept.
save_client_log() {
  if [ "${SAVE_LOG_RAW:-0}" = 1 ]; then
    docker logs --since "$2" "${3:-$CLIENT}" > "${SUITE_RUN}/logs/$1.log" 2>&1 || true
  else
    docker logs --since "$2" "${3:-$CLIENT}" 2>&1 | grep -a -v -E 'DEBUG.*hopr_transport::path::(planner|selector)' > "${SUITE_RUN}/logs/$1.log" || true
  fi
}

# ---------------------------------------------------------------------------
# cluster / nodes
# ---------------------------------------------------------------------------
cluster_status() { "${LOCALCLUSTER_BIN}" status --data-dir "${DATA_DIR}" 2>/dev/null; }
node_field() { cluster_status | python3 -c 'import sys,json; d=json.load(sys.stdin); n=d["nodes"][int(sys.argv[1])]; v=n.get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"; }
node_api_url() { node_field "$1" api_url; }
node_addr() { node_field "$1" address; }
node_pid() { node_field "$1" pid; }
node_curl() { # node_curl INDEX METHOD PATH [JSON]
  local i=$1 m=$2 p=$3 body=${4:-}
  local tok; tok=$(node_field "$i" api_token)
  local hdr=(); [ -n "$tok" ] && hdr=(-H "x-auth-token: $tok")
  if [ -n "$body" ]; then
    curl -s -m 60 -X "$m" "${hdr[@]}" -H 'content-type: application/json' "$(node_api_url "$i")$p" -d "$body"
  else
    curl -s -m 60 -X "$m" "${hdr[@]}" "$(node_api_url "$i")$p"
  fi
}
node_metrics() { node_curl "$1" GET /metrics; }
node_metric() { node_metrics "$1" | awk -v n="$2" '$1==n {print $2; exit}'; }
node_log_file() { echo "${DATA_DIR}/logs/hoprd_$1.log"; }

# node_ping FROM TO_INDEX -> latency ms or "NA"
node_ping() {
  node_curl "$1" POST "/api/v4/peers/$(node_addr "$2")/ping" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("latency","NA"))
except Exception: print("NA")'
}

# node_sampler_start NAME INTERVAL -> samples node metrics + per-pid CPU to samples/NAME.jsonl
node_sampler_start() {
  local name=$1 iv=${2:-1}
  local pids urls; pids=""; urls=""
  local i; for i in $(seq 0 $((CLUSTER_SIZE-1))); do pids="$pids,$(node_pid "$i")"; urls="$urls,$(node_api_url "$i")"; done
  python3 "${SUITE_LIB_DIR}/node-sampler.py" --pids "${pids#,}" --urls "${urls#,}" --interval "$iv" \
    --containers "${CLIENT},gnosis_vpn-server-0" --out "${SUITE_RUN}/samples/${name}.jsonl" >/dev/null 2>&1 </dev/null &
  NODE_SAMPLER_PID=$!
}
node_sampler_stop() { [ -n "${NODE_SAMPLER_PID:-}" ] && kill "${NODE_SAMPLER_PID}" 2>/dev/null || true; NODE_SAMPLER_PID=""; }

# ---------------------------------------------------------------------------
# inter-node latency (live, no cluster restart)
# ---------------------------------------------------------------------------
# On the single-host localcluster every node's P2P address is the Docker gateway, which routes via lo, so
# tc netem on lo with a per-destination-port filter shapes one relay's traffic live. This replaces restarting
# the cluster with hoprd-localcluster --latency (which rebuilds the chain) for T09-impairment-ladder/T17-latency-matrix — same method as T20-fault-injection and
# as the original DO experiments. Needs root (the suite runs as root on the test host).
: "${NETEM_IFACE:=lo}"

node_p2p_port() { cluster_status | python3 -c 'import sys,json; d=json.load(sys.stdin); print((d["nodes"][int(sys.argv[1])].get("p2p") or ":").rsplit(":",1)[1])' "$1"; }

# netem_apply "IDX=MS IDX=MS ..." — one-way delay (ms) on traffic to each node index; empty clears
netem_apply() {
  tc qdisc del dev "$NETEM_IFACE" root 2>/dev/null || true
  [ -z "${1:-}" ] && return 0
  tc qdisc add dev "$NETEM_IFACE" root handle 1: prio bands 16 || return 1
  local band=3 kv idx ms port
  for kv in $1; do
    idx=${kv%=*}; ms=${kv#*=}; port=$(node_p2p_port "$idx")
    [ -n "$port" ] || { suite_log "netem: no p2p port for node $idx"; continue; }
    tc qdisc add dev "$NETEM_IFACE" parent 1:$band handle ${band}0: netem delay "${ms}ms" limit 20000
    tc filter add dev "$NETEM_IFACE" protocol ip parent 1:0 prio 1 u32 match ip dport "$port" 0xffff flowid 1:$band
    suite_log "netem: node $idx (:$port) +${ms}ms"; band=$((band+1))
  done
}
netem_clear() { tc qdisc del dev "$NETEM_IFACE" root 2>/dev/null || true; }

# ---------------------------------------------------------------------------
# traffic target
# ---------------------------------------------------------------------------
: "${TARGET_NETWORK:=gnosis-vpn-target}"
target_ip() { docker inspect "${TARGET_NAME}" 2>/dev/null | jq -r ".[0].NetworkSettings.Networks[\"${TARGET_NETWORK}\"].IPAddress // empty"; }
target_ip_direct() { docker inspect "${TARGET_NAME}" 2>/dev/null | jq -r ".[0].NetworkSettings.Networks[\"${DOCKER_NETWORK}\"].IPAddress // empty"; }
require_target() {
  TARGET_IP=$(target_ip); TARGET_IP_DIRECT=$(target_ip_direct)
  if [ -z "${TARGET_IP}" ]; then suite_log "target container ${TARGET_NAME} not running (just target-start)"; return 1; fi
}

# curl_down HOST BYTES CAP -> JSON {code,bytes,elapsed,ttfb,mbit,complete}
curl_down() {
  local host=$1 bytes=$2 cap=$3
  local w; w=$(in_client "curl -s -o /dev/null -m $cap -w '%{http_code} %{size_download} %{time_total} %{time_starttransfer}' 'http://$host:8899/down?bytes=$bytes' 2>/dev/null || true")
  python3 -c 'import sys
p=(sys.argv[1].split()+["0","0","0","0"])[:4]; want=int(sys.argv[2])
code=p[0]; b=int(float(p[1] or 0)); t=float(p[2] or 0); ttfb=float(p[3] or 0)
mbit=round(b*8/t/1e6,3) if t>0 else 0
print(__import__("json").dumps({"code":code,"bytes":b,"elapsed":round(t,2),"ttfb":round(ttfb,2),"mbit":mbit,"complete":b>=want}))' "$w" "$bytes"
}
# curl_up HOST BYTES CAP -> same shape (bytes = size_upload)
curl_up() {
  local host=$1 bytes=$2 cap=$3
  in_client "[ -f /tmp/up.bin ] && [ \$(stat -c %s /tmp/up.bin) -eq $bytes ] || head -c $bytes /dev/zero > /tmp/up.bin"
  local w; w=$(in_client "curl -s -o /dev/null -m $cap -w '%{http_code} %{size_upload} %{time_total} %{time_starttransfer}' -H 'Content-Type: application/octet-stream' --data-binary @/tmp/up.bin 'http://$host:8899/up' 2>/dev/null || true")
  python3 -c 'import sys
p=(sys.argv[1].split()+["0","0","0","0"])[:4]; want=int(sys.argv[2])
code=p[0]; b=int(float(p[1] or 0)); t=float(p[2] or 0); ttfb=float(p[3] or 0)
mbit=round(b*8/t/1e6,3) if t>0 else 0
print(__import__("json").dumps({"code":code,"bytes":b,"elapsed":round(t,2),"ttfb":round(ttfb,2),"mbit":mbit,"complete":(b>=want and code=="200")}))' "$w" "$bytes"
}

# per-second rx/tx byte sampler on the tunnel interface, inside the client
persec_start() { # persec_start NAME
  local f="${SUITE_RUN_IN_CLIENT}/persec-$1.csv"
  in_client "rm -f $f; nohup sh -c 'while [ -d /sys/class/net/${WG_IFACE} ]; do echo \$(date +%s),\$(cat /sys/class/net/${WG_IFACE}/statistics/rx_bytes),\$(cat /sys/class/net/${WG_IFACE}/statistics/tx_bytes) >> $f; sleep 1; done' >/dev/null 2>&1 & echo \$! > /tmp/persec.pid"
}
persec_stop() { in_client 'kill $(cat /tmp/persec.pid 2>/dev/null) 2>/dev/null; true' ; }
# persec_stall NAME rx|tx -> longest run of zero-progress seconds
persec_stall() {
  python3 - "${SUITE_RUN}/persec-$1.csv" "$2" <<'PY'
import sys
try:
    rows=[l.strip().split(",") for l in open(sys.argv[1]) if l.strip()]
except FileNotFoundError:
    print(0); sys.exit()
col=1 if sys.argv[2]=="rx" else 2
prev=None; run=0; best=0
for r in rows:
    v=int(r[col])
    if prev is not None:
        run = run+1 if v==prev else 0
        best=max(best,run)
    prev=v
print(best)
PY
}

# transfer_series LABEL HOST REPS BYTES CAP -> emits one row per transfer, prints summary JSON
transfer_series() {
  local label=$1 host=$2 reps=$3 bytes=$4 cap=$5 test=${6:-T04-fixed-throughput}
  local r d u dm=() um=() dc=0 uc=0
  for r in $(seq 1 "$reps"); do
    persec_start "${label}-d$r"; d=$(curl_down "$host" "$bytes" "$cap"); persec_stop
    emit_row "$test" label="$label" dir=down rep="$r" "res=$d" stall_s="$(persec_stall "${label}-d$r" rx)"
    dm+=("$(json_get "$d" mbit)"); [ "$(json_get "$d" complete)" = "True" ] && dc=$((dc+1))
    persec_start "${label}-u$r"; u=$(curl_up "$host" "$bytes" "$cap"); persec_stop
    emit_row "$test" label="$label" dir=up rep="$r" "res=$u" stall_s="$(persec_stall "${label}-u$r" tx)"
    um+=("$(json_get "$u" mbit)"); [ "$(json_get "$u" complete)" = "True" ] && uc=$((uc+1))
  done
  python3 -c 'import sys,json,statistics as st
dm=[float(x) for x in sys.argv[1].split()]; um=[float(x) for x in sys.argv[2].split()]
print(json.dumps({"down_median":round(st.median(dm),3),"up_median":round(st.median(um),3),"down_complete":int(sys.argv[3]),"up_complete":int(sys.argv[4]),"reps":len(dm)}))' "${dm[*]}" "${um[*]}" "$dc" "$uc"
}


usage_common() {
  cat <<USAGE
Common environment: CLIENT=${CLIENT} DEST=${DEST} TARGET_NAME=${TARGET_NAME} SUITE_RUN=${SUITE_RUN}
  SUITE_FAST=${SUITE_FAST} (1 shortens durations)  DEADMAN=${DEADMAN}s  BYTES=${BYTES} CAP=${CAP}s REPS=${REPS}
Run through 'just suite <profile>' or 'just test <tNN>' so the stack variables are exported.
USAGE
}
