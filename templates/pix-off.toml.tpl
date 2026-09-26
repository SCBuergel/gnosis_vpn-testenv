# PIX is on for the main tunnel session by default, and a stock localcluster cannot settle it. The
# exchange itself is fine — the exit accepts the client's parameters — but settlement needs a `Pix`
# strategy on the exit, which is opt-in and which `hoprd-localcluster` only installs with
# `--enable-pix`. Without it the exit fails the SSA request ("deposit pool did not supply deposit
# data … has no listener"), poisons the session's service gate and closes the tunnel, so no
# destination ever finishes connecting.
#
# `just up-pix` starts a cluster that can settle it and generates the matching client config;
# `just system-test-pix` then drives a full deposit → recover → sweep cycle and asserts on it.
[connection.pix.ping_main]
enabled = false
