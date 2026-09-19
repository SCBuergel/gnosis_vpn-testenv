#!/usr/bin/env python3
"""One-directional paced UDP stream through the tunnel. --mode ul: send to the stream server, which
measures. --mode dl: ask the server to stream to us and measure here. Stall = gap > 1 s in the received
stream while the sender continued. Delay is one-way delay above the minimum observed (clock offset cancels).

The socket is RE-BOUND whenever the tunnel interface is recreated (the client's watchdog reconnect): a
socket bound to the removed interface goes blind without error, which reads as loss, an UNMEASURED download
arm, or an upload whose end-of-stream report "never arrived" — all three were seen on 2026-09-19 for this
one cause. The local port is kept across rebinds so the server keeps streaming to the same address, and
the dl keepalive lets the server re-learn the address if it did change. Rebinds and the outage they caused
are reported separately from loss."""
import argparse, json, random, socket, struct, threading, time
ap=argparse.ArgumentParser()
ap.add_argument("--mode",choices=["ul","dl"],required=True); ap.add_argument("--host",required=True); ap.add_argument("--port",type=int,default=8902)
ap.add_argument("--rate-mbit",type=float,required=True); ap.add_argument("--duration",type=float,required=True)
ap.add_argument("--size",type=int,default=1200); ap.add_argument("--iface",default="wg0_gnosisvpn"); ap.add_argument("--out",required=True)
ap.add_argument("--local-port",type=int,default=0,help="fixed local port (0 = pick once, then keep it across rebinds)")
ap.add_argument("--dump",default=None,help="dl mode: write seq,delay_ms per packet")
a=ap.parse_args()
dst=(a.host,a.port); sid=random.getrandbits(32); pps=a.rate_mbit*1e6/8/a.size; interval=1.0/pps
stop=threading.Event()
state={"sock":None,"ifindex":None,"port":a.local_port,"rebinds":0,"outages":[],"outage_from":None,"last_recv":None,"events":[]}

def ifindex():
    try: return socket.if_nametoindex(a.iface)
    except OSError: return None

def make_socket(timeout):
    sk=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    sk.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    sk.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,a.iface.encode())
    sk.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,8<<20); sk.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,8<<20)
    sk.bind(("0.0.0.0",state["port"])); state["port"]=sk.getsockname()[1]
    sk.settimeout(timeout)
    return sk

def ev(name): state["events"].append((round(time.time(),3),name))

def watcher(timeout):
    while not stop.is_set():
        time.sleep(0.5)
        idx=ifindex()
        if idx==state["ifindex"]: continue
        if idx is None:
            if state["ifindex"] is not None:
                ev("IFDOWN")
                if state["outage_from"] is None: state["outage_from"]=state["last_recv"] or time.time()
            state["ifindex"]=None
            continue
        try: new=make_socket(timeout)
        except OSError as e:
            ev("REBIND_FAILED %s"%e); continue
        old=state["sock"]; state["sock"]=new; state["ifindex"]=idx; state["rebinds"]+=1
        if state["outage_from"] is None: state["outage_from"]=state["last_recv"] or time.time()
        ev("REBIND ifindex=%s"%idx)
        try: old.close()
        except OSError: pass

def note_recv(now):
    if state["outage_from"] is not None:
        state["outages"].append((round(state["outage_from"],3),round(now,3),round(now-state["outage_from"],3))); state["outage_from"]=None
    state["last_recv"]=now

def pct(v,p): v=sorted(v); return round(v[min(len(v)-1,int(p*len(v)))]*1000,1) if v else None

timeout=2.0 if a.mode=="ul" else 1.0
state["ifindex"]=ifindex(); state["sock"]=make_socket(timeout)
ev("START ifindex=%s port=%d"%(state["ifindex"],state["port"]))
threading.Thread(target=watcher,args=(timeout,),daemon=True).start()
start=time.time(); summ={"mode":a.mode,"rate_mbit":a.rate_mbit,"size":a.size,"pps":round(pps,1),"sid":sid,"local_port":state["port"],"start":start}
if a.mode=="ul":
    pad=b"x"*(a.size-20); t0=time.monotonic(); n=0
    while time.monotonic()-t0<a.duration:
        target=t0+n*interval; now=time.monotonic()
        if now<target: time.sleep(min(target-now,0.01)); continue
        try: state["sock"].sendto(b"UPLD"+struct.pack("!IId",sid,n,time.time())+pad,dst)
        except OSError: pass
        n+=1
    send_end=time.time(); time.sleep(2.0); rep=None
    for _ in range(15):
        sk=state["sock"]
        try:
            sk.sendto(b"UPRQ"+struct.pack("!IId",sid,n,send_end),dst); d,_=sk.recvfrom(65535)
            if d[:4]==b"UPRP": rep=json.loads(d[4:]); break
        except (socket.timeout,OSError): continue
    summ.update({"sent":n,"duration_s":round(send_end-start,1),"server_report":rep})
    if rep: summ.update({k:rep[k] for k in rep if k!="sent"})
else:
    recv=0; last=None; gaps=[]; delays=[]; maxseq=-1; sent_total=None; send_end=None; lastkeep=0
    for _ in range(3):
        try: state["sock"].sendto(b"CTLD"+struct.pack("!IfII",sid,pps,a.size,int(a.duration)),dst)
        except OSError: pass
        time.sleep(0.3)
    deadline=time.monotonic()+a.duration+15
    while time.monotonic()<deadline:
        if time.time()-lastkeep>3:
            try: state["sock"].sendto(b"KEEP"+struct.pack("!I",sid),dst)
            except OSError: pass
            lastkeep=time.time()
        sk=state["sock"]
        try: d,_=sk.recvfrom(65535)
        except socket.timeout: continue
        except OSError: time.sleep(0.05); continue
        now=time.time(); tag=d[:4]
        if tag==b"DLDA" and len(d)>=20:
            psid,seq,ts=struct.unpack("!IId",d[4:20])
            if psid!=sid: continue
            recv+=1; delays.append(now-ts); maxseq=max(maxseq,seq)
            if last is not None and now-last>1.0: gaps.append((round(last,3),round(now-last,3)))
            last=now; note_recv(now)
        elif tag==b"DLND" and len(d)>=20:
            psid,sent_total,send_end=struct.unpack("!IId",d[4:20]); break
    if sent_total is None: sent_total=maxseq+1; send_end=start+a.duration+1.0; summ["end_marker"]="lost"
    if last is not None and send_end-last>1.0: gaps.append((round(last,3),round(send_end-last,3)))
    m=min(delays) if delays else 0; dn=[x-m for x in delays]
    if a.dump: open(a.dump,"w").write("".join("%d,%.1f\n"%(i,x*1000) for i,x in enumerate(dn)))
    summ.update({"sent":sent_total,"recv":recv,"duration_s":round(time.time()-start,1),"loss_pct":round(100*(1-recv/max(1,sent_total)),2),
        "delay_over_min_ms":{"p50":pct(dn,.5),"p90":pct(dn,.9),"p99":pct(dn,.99),"max":pct(dn,1.0)},"delayed_pkts_gt_1s":sum(1 for x in dn if x>1.0),
        "stalls_gt_1s":len(gaps),"stalls_gt_2s":sum(1 for g in gaps if g[1]>2),"stalls_gt_5s":sum(1 for g in gaps if g[1]>5),
        "stall_total_s":round(sum(g[1] for g in gaps),1),"worst_stalls":sorted(gaps,key=lambda g:-g[1])[:10]})
stop.set()
if state["outage_from"] is not None:   # never recovered inside the arm
    state["outages"].append((round(state["outage_from"],3),None,round(time.time()-state["outage_from"],3))); state["outage_from"]=None
# In ul mode nothing arrives until the end-of-stream report, so the client cannot time an outage; the server's gap list
# (stalls_gt_*s / worst_stalls in its report) is the outage view for that direction. Report None rather than a number
# that would only measure "time from the interface going away to the report".
if a.mode=="ul": state["outages"]=[]; outage_total=None
else: outage_total=round(sum(o[2] for o in state["outages"]),1)
summ.update({"rebinds":state["rebinds"],"outages":state["outages"],"outage_total_s":outage_total,"events":state["events"]})
summ["end"]=time.time(); json.dump(summ,open(a.out,"w"),indent=1); print(json.dumps(summ))
