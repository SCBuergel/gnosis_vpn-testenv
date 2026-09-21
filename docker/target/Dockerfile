# In-cluster traffic target for the regression suite. It is a container only so it can sit on the far side of the
# exit: on its own Docker network with a non-RFC1918 subnet the exit NATs into (the client keeps private ranges off
# the tunnel). The services themselves are plain Python modules; scripts/suite/selftest runs them on loopback.
#   :8899/tcp  sized download (/down?bytes=N), upload sink (/up), /health   (speedtarget.py)
#   :8901/udp  bidirectional UDP echo                                        (callecho.py)
#   :8902/udp  one-directional paced UDP stream server, UL/DL                (streamsrv.py)
#   :8903/udp  two-way call simulator with per-packet CSV log + idle-pause   (callsrv.py)
FROM python:3.12-slim
WORKDIR /app
COPY udpserver.py speedtarget.py callecho.py streamsrv.py callsrv.py main.py ./
RUN mkdir -p /root/callsrv
EXPOSE 8899/tcp 8901/udp 8902/udp 8903/udp
CMD ["python3", "-u", "main.py"]
