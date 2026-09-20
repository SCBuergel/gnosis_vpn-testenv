#!/usr/bin/env bash
# T16-metric-sampling — Node metric sampling during load (diagnostic; absorbs the former health-check-load test as a covariate): 1 Hz samples of every node's counters and CPU, the client and
# server container CPU, plus the client's balancer estimate, during one T04-fixed-throughput download+upload; then summary assertions:
# exit-side session target reaches the client's main target, relay egress ring drops flat, rejected counters flat,
# and the balancer-level vs per-second-throughput correlation (R10 rule: report the slope, not the gauge).
source "$(dirname "$0")/lib.sh"
suite_kind diagnostic
[ "${1:-}" = "--help" ] && { sed -n '2,6p' "$0"; usage_common; exit 0; }
suite_init; require_target
connect "$DEST" 5 || { verdict T16-metric-sampling FAIL "connect failed"; exit 1; }
node_sampler_start t16 1
# client balancer estimate at 1 Hz alongside per-second rx bytes
in_client_bg "i=0; while [ \$i -lt 90 ]; do echo \$(date +%s),\$(cat /sys/class/net/$WG_IFACE/statistics/rx_bytes),\$(gnosis_vpn-ctl -o json telemetry 2>/dev/null | tr -d '\\\\' | grep -oE 'hopr_surb_balancer_current_buffer_estimate[^ ]* [0-9.]+' | head -1 | awk '{print \$2}') >> ${SUITE_RUN_IN_CLIENT}/t16-client.csv; sleep 1; i=\$((i+1)); done"
sleep 10; r=$(curl_down "$TARGET_IP" "$((BYTES*2))" "$CAP"); u=$(curl_up "$TARGET_IP" "$BYTES" "$CAP"); sleep 3
node_sampler_stop; disconnect
an=$(python3 - "$SUITE_RUN/samples/t16.jsonl" "$SUITE_RUN/t16-client.csv" <<'PY'
import sys,json,statistics as st
rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
def series(i,key,agg=sum):
    out=[]
    for r in rows:
        n=r["nodes"][i] if i<len(r["nodes"]) else {}
        v=[val for k,val in n.items() if k.startswith(key)]
        out.append(agg(v) if v else None)
    return [x for x in out if x is not None]
res={}
for i in range(len(rows[0]["nodes"]) if rows else 0):
    drop=series(i,"hopr_egress_ring_buffer_dropped"); rej=series(i,"hopr_packet_rejected_count")
    fwd=series(i,'hopr_packets_count{type="forwarded"}'); sent=series(i,'hopr_packets_count{type="sent"}')
    cpu=[r["cpu_pct"].get(f"node{i}") for r in rows if r["cpu_pct"].get(f"node{i}") is not None]
    res[f"node{i}"]={"egress_drop_delta":(drop[-1]-drop[0]) if drop else None,"rejected_delta":(rej[-1]-rej[0]) if rej else None,
        "forwarded_pps_max":max((b-a) for a,b in zip(fwd,fwd[1:])) if len(fwd)>1 else None,"sent_pps_max":max((b-a) for a,b in zip(sent,sent[1:])) if len(sent)>1 else None,
        "cpu_pct_mean":round(st.mean(cpu),1) if cpu else None,"cpu_pct_max":max(cpu) if cpu else None}
# the exit publishes its balancer per session as hopr_surb_balancer_current_buffer_{target,estimate}{session_id=...}
# (hopr_session_surb_* are the CLIENT's names; reading them here returned None in every run). Max over sessions,
# not the sum: closed sessions stay in the gauge for hours (R11).
tgt=series(0,"hopr_surb_balancer_current_buffer_target",max); est=series(0,"hopr_surb_balancer_current_buffer_estimate",max)
res["exit_session_target_max"]=max(tgt) if tgt else None; res["exit_session_estimate_max"]=max(est) if est else None
ccpu={}
for r in rows:
    for k,v in r.get("container_cpu_pct",{}).items(): ccpu.setdefault(k,[]).append(v)
res["container_cpu_mean"]={k:round(st.mean(v),1) for k,v in ccpu.items()}
# balancer level vs throughput
try:
    cl=[l.strip().split(",") for l in open(sys.argv[2]) if l.strip()]
    pts=[]
    for a,b in zip(cl,cl[1:]):
        try:
            if len(a)>=3 and a[2] and b[2] and a[1] and b[1]:
                pts.append((float(a[2]), (int(b[1])-int(a[1]))*8/1e6))
        except ValueError:
            continue   # the interface vanished for a sample (reconnect): empty fields
    if len(pts)>5:
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
        mx,my=st.mean(xs),st.mean(ys); sxy=sum((x-mx)*(y-my) for x,y in pts); sxx=sum((x-mx)**2 for x in xs); syy=sum((y-my)**2 for y in ys)
        res["balancer_vs_throughput"]={"n":len(pts),"r":round(sxy/((sxx*syy)**0.5),3) if sxx>0 and syy>0 else None,"slope_mbit_per_100_surbs":round(100*sxy/sxx,4) if sxx>0 else None}
except FileNotFoundError:
    pass
print(json.dumps(res))
PY
)
# covariate (former health-check-load test): how many health-check sessions the exit served during the window — the cost of the
# client's own destination list, recorded next to the throughput rather than gated on
hc=$(grep -c "got new session request" "$(node_log_file 0)" 2>/dev/null || true)
emit_row T16-metric-sampling kind=summary "download=$r" "upload=$u" "analysis=$an" exit_session_requests_total="${hc:-0}" destinations="$(client_status_text | grep -c 'Route health:' || true)"
record T16-metric-sampling "sampled $(wc -l < "$SUITE_RUN/samples/t16.jsonl") s; $(echo "$an" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("exit target max", d.get("exit_session_target_max"), "estimate max", d.get("exit_session_estimate_max"), "; node0 sent pps max", d.get("node0",{}).get("sent_pps_max"), "; balancer r", (d.get("balancer_vs_throughput") or {}).get("r"), "; container cpu", d.get("container_cpu_mean"))')"
exit 0
