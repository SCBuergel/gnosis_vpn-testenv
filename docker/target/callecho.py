import socket, os
port=int(os.environ.get("PORT","8901"))
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,8<<20)
s.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,8<<20)
s.bind(("0.0.0.0",port))
print(f"callecho udp 0.0.0.0:{port}",flush=True)
while True:
    try:
        d,a=s.recvfrom(65535); s.sendto(d,a)
    except Exception: pass
