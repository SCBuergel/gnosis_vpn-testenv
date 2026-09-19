#!/usr/bin/env python3
"""Minimal paced UDP echo load: send SIZE-byte datagrams at RATE Mbit/s to an echo server, count the echoes.
Prints one line per second (+t, sent this second, echoed this second) and a summary. The socket is deliberately
UNBOUND: inside the client container the only route to the target is the tunnel, so the flow follows the
WireGuard interface across the client's reconnects without any re-bind logic."""
import argparse, socket, struct, threading, time
ap=argparse.ArgumentParser()
ap.add_argument("--host",required=True); ap.add_argument("--port",type=int,default=8901)
ap.add_argument("--rate-mbit",type=float,default=1.5); ap.add_argument("--size",type=int,default=1200)
ap.add_argument("--duration",type=float,default=300)
a=ap.parse_args()
dst=(a.host,a.port); interval=a.size*8/(a.rate_mbit*1e6); pace=int(1/interval); pad=b"x"*(a.size-8)
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(0.2)
sent=recv=0; per={}; stop=False; t0=time.time()

def receiver():
    global recv
    while not stop:
        try: d,_=s.recvfrom(65535)
        except (socket.timeout,OSError): continue
        recv+=1; per.setdefault(int(time.time()-t0),[0,0])[1]+=1

threading.Thread(target=receiver,daemon=True).start()
n=0; next_line=1
while True:
    now=time.time()
    if now-t0>=a.duration: break
    due=t0+n*interval
    if now<due: time.sleep(min(due-now,0.005)); continue
    try: s.sendto(struct.pack("!Q",n)+pad,dst); sent+=1; per.setdefault(int(now-t0),[0,0])[0]+=1
    except OSError: pass          # tunnel gone: kill switch drops it, keep pacing
    n+=1
    if now-t0>=next_line:
        sec=next_line-1; c=per.get(sec,[0,0]); print("+%3ds  sent %3d  echoed %3d%s"%(sec,c[0],c[1],"   <- nothing came back" if c[0] and not c[1] else ""),flush=True); next_line+=1
time.sleep(2); stop=True
down=sum(1 for sec in range(int(a.duration)) if per.get(sec,[0,0])[0]<pace//2)   # sends failing: tunnel gone
print("summary: %.0fs, sent %d, echoed %d (%.1f%% of sent lost), %d seconds with the tunnel gone (sends failing)"%(time.time()-t0,sent,recv,100*(1-recv/max(1,sent)),down),flush=True)
