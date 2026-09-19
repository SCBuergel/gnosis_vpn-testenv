#!/usr/bin/env python3
"""Paced bidirectional UDP echo probe: sends a constant-rate stream to an echo server (upload) and
receives the echoes (download). Reports loss, RTT tail, and "stalls": gaps > 1 s in the echo stream
while sending continued. Bound to an interface with SO_BINDTODEVICE so it fails closed off-tunnel.

The socket is RE-BOUND whenever the interface is recreated (the client's watchdog reconnect tears the
WireGuard interface down and creates a new one a few seconds later). A socket bound to the removed
interface goes blind without any error, and blind is indistinguishable from loss: on 2026-09-19 every
T06-realtime-udp arm read "loss = (arm length - 50 s) / arm length" for exactly this reason. Each rebind is
counted and the outage it caused (last echo before, first echo after) is reported next to, not inside,
the loss figure. The local port is kept across rebinds so the far end's view of us does not change."""
import argparse, json, socket, struct, threading, time, statistics
ap=argparse.ArgumentParser()
ap.add_argument("--host",required=True); ap.add_argument("--port",type=int,default=8901)
ap.add_argument("--rate-mbit",type=float,required=True); ap.add_argument("--duration",type=float,required=True)
ap.add_argument("--size",type=int,default=1200); ap.add_argument("--iface",default="wg0_gnosisvpn")
ap.add_argument("--local-port",type=int,default=0,help="fixed local port (0 = pick once, then keep it across rebinds)")
ap.add_argument("--out",required=True)
a=ap.parse_args()
dst=(a.host,a.port)
pps=a.rate_mbit*1e6/8/a.size; interval=1.0/pps
pad=b"x"*(a.size-12)
stop=threading.Event(); lock=threading.Lock()
st={"sent":0,"recv":0,"dup":0,"rtts":[],"delayed":0,"gaps":[],"last_recv":None,"persec":{},
    "rebinds":0,"outages":[],"outage_from":None,"events":[]}
seen=set()
state={"sock":None,"ifindex":None,"port":a.local_port}

def ifindex():
    try: return socket.if_nametoindex(a.iface)
    except OSError: return None

def make_socket():
    sk=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    sk.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    sk.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,a.iface.encode())
    sk.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,8<<20); sk.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,8<<20)
    sk.bind(("0.0.0.0",state["port"])); state["port"]=sk.getsockname()[1]
    sk.settimeout(0.2)
    return sk

def ev(name):
    st["events"].append((round(time.time(),3),name))

def watcher():
    """Rebuild the socket when the tunnel interface is recreated; note when it went away."""
    while not stop.is_set():
        time.sleep(0.5)
        idx=ifindex()
        if idx==state["ifindex"]: continue
        if idx is None:
            if state["ifindex"] is not None:
                ev("IFDOWN")
                with lock:
                    if st["outage_from"] is None: st["outage_from"]=st["last_recv"] or time.time()
            state["ifindex"]=None
            continue
        try: new=make_socket()
        except OSError as e:
            ev("REBIND_FAILED %s"%e); continue
        old=state["sock"]; state["sock"]=new; state["ifindex"]=idx
        with lock:
            st["rebinds"]+=1
            if st["outage_from"] is None: st["outage_from"]=st["last_recv"] or time.time()
        ev("REBIND ifindex=%s"%idx)
        try: old.close()
        except OSError: pass

def sender():
    t0=time.monotonic(); n=0
    while not stop.is_set():
        now=time.monotonic()
        if now-t0>=a.duration: break
        target=t0+n*interval
        if now<target: time.sleep(min(target-now,0.01)); continue
        try: state["sock"].sendto(struct.pack("!Id",n,time.time())+pad,dst)
        except OSError: pass
        n+=1
        with lock: st["sent"]=n
    st["send_end"]=time.time()
    stop.set()

def receiver():
    while not stop.is_set():
        sk=state["sock"]
        try: d,_=sk.recvfrom(65535)
        except socket.timeout: continue
        except OSError: time.sleep(0.05); continue
        if len(d)<12: continue
        now=time.time(); seq,ts=struct.unpack("!Id",d[:12]); rtt=now-ts
        with lock:
            if seq in seen: st["dup"]+=1; continue
            seen.add(seq); st["recv"]+=1; st["rtts"].append(rtt)
            if rtt>1.0: st["delayed"]+=1
            if st["last_recv"] is not None:
                gap=now-st["last_recv"]
                if gap>1.0: st["gaps"].append((round(st["last_recv"],3),round(gap,3)))
            if st["outage_from"] is not None:
                st["outages"].append((round(st["outage_from"],3),round(now,3),round(now-st["outage_from"],3)))
                st["outage_from"]=None
            st["last_recv"]=now
            sec=int(now); ps=st["persec"].setdefault(sec,[0,[]]); ps[0]+=1; ps[1].append(rtt)

state["ifindex"]=ifindex(); state["sock"]=make_socket()
ev("START ifindex=%s port=%d"%(state["ifindex"],state["port"]))
tW=threading.Thread(target=watcher,daemon=True); tS=threading.Thread(target=sender,daemon=True); tR=threading.Thread(target=receiver,daemon=True)
start=time.time(); tW.start(); tS.start(); tR.start(); tS.join(); time.sleep(2.0); stop.set(); tR.join(1.0)
end=time.time()
with lock:
    se=st.get("send_end",end)
    if st["last_recv"] is not None and se-st["last_recv"]>1.0: st["gaps"].append((round(st["last_recv"],3),round(se-st["last_recv"],3)))
    if st["outage_from"] is not None:   # never recovered inside the arm
        st["outages"].append((round(st["outage_from"],3),None,round(se-st["outage_from"],3))); st["outage_from"]=None
    r=sorted(st["rtts"]); q=lambda p: (r[min(len(r)-1,int(p*len(r)))] if r else None)
    gaps=sorted(st["gaps"],key=lambda g:-g[1])
    summ={"host":a.host,"iface":a.iface,"local_port":state["port"],"rate_mbit":a.rate_mbit,"size":a.size,"pps":round(pps,1),"duration_s":round(end-start,1),
          "sent":st["sent"],"recv":st["recv"],"dup":st["dup"],"loss_pct":round(100*(1-st["recv"]/max(1,st["sent"])),2),
          "rtt_ms":{"p50":q(.5) and round(q(.5)*1000,1),"p90":q(.9) and round(q(.9)*1000,1),"p99":q(.99) and round(q(.99)*1000,1),"max":r and round(r[-1]*1000,1)},
          "delayed_pkts_rtt_gt_1s":st["delayed"],
          "stalls_gt_1s":len(st["gaps"]),"stalls_gt_2s":sum(1 for g in st["gaps"] if g[1]>2),"stalls_gt_5s":sum(1 for g in st["gaps"] if g[1]>5),
          "stall_total_s":round(sum(g[1] for g in st["gaps"]),1),"worst_stalls":gaps[:10],
          "rebinds":st["rebinds"],"outages":st["outages"],"outage_total_s":round(sum(o[2] for o in st["outages"]),1),
          "events":st["events"],"start":start,"end":end}
    with open(a.out+".csv","w") as f:
        f.write("t,recv,rtt_p50_ms,rtt_max_ms\n")
        for sec in range(int(start),int(end)+1):
            n,rl=st["persec"].get(sec,[0,[]]); f.write("%d,%d,%s,%s\n"%(sec,n, round(statistics.median(rl)*1000,1) if rl else "", round(max(rl)*1000,1) if rl else ""))
json.dump(summ,open(a.out+".json","w"),indent=1); print(json.dumps(summ))
