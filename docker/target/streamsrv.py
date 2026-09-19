#!/usr/bin/env python3
"""One-directional UDP stream server. DL: on a CTLD request, streams paced packets to the requester.
UL: counts UPLD packets per session (gaps > 1 s, one-way delay above the minimum) and answers UPRQ with JSON."""
import socket, struct, time, json, os, threading
PORT=int(os.environ.get("PORT","8902"))
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,8<<20); s.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,8<<20)
s.bind(("0.0.0.0",PORT)); print(f"streamsrv udp 0.0.0.0:{PORT}",flush=True)
ul={}; dl_started=set(); dl_addr={}; lock=threading.Lock()
# dl_addr[sid] is re-learned from every KEEP the requester sends, so a client whose tunnel reconnected (and whose
# source address may have changed) keeps receiving the stream instead of the server streaming into the void.
def dl_stream(addr,sid,pps,size,dur):
    interval=1.0/pps; pad=b"x"*max(0,size-20); t0=time.monotonic(); n=0
    while time.monotonic()-t0<dur:
        target=t0+n*interval; now=time.monotonic()
        if now<target: time.sleep(min(target-now,0.01)); continue
        try: s.sendto(b"DLDA"+struct.pack("!IId",sid,n,time.time())+pad,dl_addr.get(sid,addr))
        except OSError: pass
        n+=1
    for _ in range(8):
        try: s.sendto(b"DLND"+struct.pack("!IId",sid,n,time.time()),dl_addr.get(sid,addr))
        except OSError: pass
        time.sleep(0.25)
    print(f"DL sid={sid} to {dl_addr.get(sid,addr)} sent={n}",flush=True)
def pct(v,p): v=sorted(v); return round(v[min(len(v)-1,int(p*len(v)))]*1000,1) if v else None
while True:
    try: d,a=s.recvfrom(65535)
    except Exception: continue
    tag=d[:4]; now=time.time()
    if tag==b"UPLD" and len(d)>=20:
        sid,seq,ts=struct.unpack("!IId",d[4:20])
        with lock:
            st=ul.setdefault(sid,{"recv":0,"last":None,"first":now,"gaps":[],"delays":[],"maxseq":-1})
            st["recv"]+=1; st["delays"].append(now-ts); st["maxseq"]=max(st["maxseq"],seq)
            if st["last"] is not None and now-st["last"]>1.0: st["gaps"].append((round(st["last"],3),round(now-st["last"],3)))
            st["last"]=now
    elif tag==b"CTLD" and len(d)>=20:
        sid,pps,size,dur=struct.unpack("!IfII",d[4:20])
        dl_addr[sid]=a
        if sid not in dl_started:
            dl_started.add(sid); threading.Thread(target=dl_stream,args=(a,sid,pps,size,dur),daemon=True).start()
            print(f"DL start sid={sid} {a} pps={pps} size={size} dur={dur}",flush=True)
    elif tag==b"KEEP" and len(d)>=8:
        sid=struct.unpack("!I",d[4:8])[0]
        if sid in dl_started and dl_addr.get(sid)!=a:
            print(f"DL sid={sid} requester moved {dl_addr.get(sid)} -> {a}",flush=True); dl_addr[sid]=a
    elif tag==b"UPRQ" and len(d)>=20:
        sid,sent,send_end=struct.unpack("!IId",d[4:20])
        with lock: st=ul.get(sid)
        if st is None: rep={"recv":0,"error":"unknown session"}
        else:
            gaps=list(st["gaps"])
            if st["last"] is not None and send_end-st["last"]>1.0: gaps.append((round(st["last"],3),round(send_end-st["last"],3)))
            m=min(st["delays"]) if st["delays"] else 0; dn=[x-m for x in st["delays"]]
            rep={"recv":st["recv"],"sent":sent,"loss_pct":round(100*(1-st["recv"]/max(1,sent)),2),
                 "delay_over_min_ms":{"p50":pct(dn,.5),"p90":pct(dn,.9),"p99":pct(dn,.99),"max":pct(dn,1.0)},
                 "delayed_pkts_gt_1s":sum(1 for x in dn if x>1.0),
                 "stalls_gt_1s":len(gaps),"stalls_gt_2s":sum(1 for g in gaps if g[1]>2),"stalls_gt_5s":sum(1 for g in gaps if g[1]>5),
                 "stall_total_s":round(sum(g[1] for g in gaps),1),"worst_stalls":sorted(gaps,key=lambda g:-g[1])[:3]}
            # keep the reply well under the tunnel MTU: a fragmented UDP reply rarely survives the return path
        try: s.sendto(b"UPRP"+json.dumps(rep).encode(),a)
        except OSError: pass
