# In-cluster traffic target for the regression suite (catalogue extension 2):
#   :8899/tcp  sized download (/down?bytes=N), upload sink (/up), /health   (speedtarget.py)
#   :8901/udp  bidirectional UDP echo                                        (callecho.py)
#   :8902/udp  one-directional paced UDP stream server, UL/DL                (streamsrv.py)
#   :8903/udp  two-way call simulator with per-packet CSV log + idle-pause   (callsrv.py)
FROM python:3.12-slim
WORKDIR /app
COPY speedtarget.py callecho.py streamsrv.py callsrv.py entrypoint.sh ./
RUN chmod +x entrypoint.sh && mkdir -p /root/callsrv
EXPOSE 8899/tcp 8901/udp 8902/udp 8903/udp
ENTRYPOINT ["./entrypoint.sh"]
