#!/usr/bin/env bash
# T31-frame-forensics — Instrumented-build frame forensics: read-length histogram and slab analysis of every inbound datagram during a
# cold start. Needs a client image built with the inbound-read instrumentation (catalogue extension 12) that logs
# 'inbound datagram ... len=N'. SKIPs when the running image does not emit those lines.
source "$(dirname "$0")/lib.sh"
suite_kind runbook
[ "${1:-}" = "--help" ] && { sed -n '2,4p' "$0"; usage_common; exit 0; }
suite_init; require_target
connect "$DEST" 0 || { verdict T31-frame-forensics FAIL "connect failed"; exit 1; }
r=$(curl_down "$TARGET_IP" "$BYTES" "$CAP"); n=$(docker logs --since "$LOG_SINCE" "$CLIENT" 2>&1 | grep -c "inbound datagram" || true); save_client_log t31 "$LOG_SINCE"; disconnect
[ "${n:-0}" -gt 0 ] || { verdict T31-frame-forensics SKIP "client image has no inbound-read instrumentation (0 'inbound datagram' lines)"; exit 0; }
an=$(python3 -c '
import sys,re,json,collections
reads=[]; fails=0
for line in open(sys.argv[1], errors="replace"):
    if "inbound datagram" not in line: continue
    m=re.search(r"len=(\d+)", line);
    if not m: continue
    n=int(m.group(1)); f="failed=true" in line; reads.append((n,f)); fails+=f
hist=collections.Counter(n for n,_ in reads); slabs=[]; cur=0
for n,_ in reads:
    cur+=n
    if n<1500: slabs.append(cur); cur=0
multi=[s for s in slabs if s>1500]
print(json.dumps({"reads":len(reads),"failed":fails,"max_read":max(hist) if hist else 0,"top":hist.most_common(5),"multi_slabs":len(multi),"max_slab":max(multi) if multi else 0}))' "$SUITE_RUN/logs/t31.log")
emit_row T31-frame-forensics "analysis=$an" "download=$r"
[ "$(json_get "$an" multi_slabs)" = 0 ] && verdict T31-frame-forensics PASS "$(json_get "$an" reads) reads, $(json_get "$an" failed) failed, max read $(json_get "$an" max_read) B, no packed slabs" || verdict T31-frame-forensics FAIL "$(json_get "$an" multi_slabs) packed slabs (max $(json_get "$an" max_slab) B), $(json_get "$an" failed)/$(json_get "$an" reads) reads failed"
exit $SUITE_FAILED
