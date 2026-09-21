"""T02-build-provenance: version strings and the compiled-in HOPR wire-protocol identifiers of the client worker
and hoprd; asserts one protocol id across the two. The exit server drives its hoprd node over REST and embeds no
wire-protocol id (checked: the binary holds no /hopr/ string), so its version is recorded and an id is compared
only if a future build carries one. Writes provenance.json."""
import hashlib
import json
import time

from suitelib import shell

TEST = "T02-build-provenance"
KIND = "gate"
KNOBS = {}


def _sha16(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return ""


def test_build_provenance(cfg, run, client, checks, knobs):
    cv = client.version()
    cp = client.out("grep -aoE -m1 '/hopr/mix/[0-9.]+' /app/gnosis_vpn-worker")
    hv = (shell.out([cfg.hoprd_bin, "--version"], timeout=30) or "unknown").splitlines()[0]
    hp = shell.out(["grep", "-aoE", "-m1", "/hopr/mix/[0-9.]+", cfg.hoprd_bin], timeout=120)
    sv = (shell.out(["docker", "exec", cfg.server, "./gnosis_vpn-server", "--version"], timeout=30) or "unknown").splitlines()[0]
    sp = shell.out(["docker", "exec", cfg.server, "sh", "-c", "grep -aoE -m1 '/hopr/mix/[0-9.]+' ./gnosis_vpn-server"], timeout=120)
    ci = shell.out(["docker", "inspect", "-f", "{{.Config.Image}} {{.Image}}", client.name], timeout=30)
    si = shell.out(["docker", "inspect", "-f", "{{.Config.Image}} {{.Image}}", cfg.server], timeout=30)
    # the glibc images carry the source commit as an OCI label (build-client-glibc/build-server-glibc REVISION=...)
    crev = shell.out(["docker", "inspect", "-f", '{{index .Config.Labels "org.opencontainers.image.revision"}}', client.name], timeout=30)
    srev = shell.out(["docker", "inspect", "-f", '{{index .Config.Labels "org.opencontainers.image.revision"}}', cfg.server], timeout=30)
    lcv = (shell.out([cfg.localcluster_bin, "--version"], timeout=30) or "").splitlines()[:1]
    d = {"client_version": cv, "client_protocol": cp, "hoprd_version": hv, "hoprd_protocol": hp, "server_version": sv, "server_protocol": sp,
         "client_image": ci, "server_image": si, "client_image_revision": crev, "server_image_revision": srev, "hoprd_sha256_16": _sha16(cfg.hoprd_bin),
         "localcluster_version": lcv[0] if lcv else "", "cluster_env": cfg.cluster_env, "cluster_latency": cfg.cluster_latency,
         "hoprd_bin": cfg.hoprd_bin, "client_image_tag": cfg.client_image, "cell": cfg.cell,
         "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    json.dump(d, open(run / "provenance.json", "w"), indent=1)
    print(json.dumps(d))
    checks.row(client=cv, client_protocol=cp, hoprd=hv, hoprd_protocol=hp, server=sv, server_protocol=sp)
    # a mismatch is the incompatibility this gate exists for; an unreadable id is a WARN because it cannot be judged
    if sv == "unknown":
        checks.warn(f"server version unreadable (is {cfg.server} running?): client {cv} ({cp}), hoprd {hv} ({hp})")
    ids = {"client": cp, "hoprd": hp}
    if sp:                       # the server embeds no id today; compare it only when a build carries one
        ids["server"] = sp
    if all(ids.values()) and len(set(ids.values())) == 1:
        checks.passed(f"one protocol id {cp} across client {cv}, hoprd {hv}" + (f", server {sv}" if sp else f" (server {sv} embeds none)"))
    elif all(ids.values()):
        checks.failed(f"protocol id mismatch: client {cv} speaks {cp}, hoprd {hv} speaks {hp}" + (f", server {sv} speaks {sp}" if sp else "")
                      + "; a HOPR packet's frame size is fixed, so mismatched versions misparse rather than refuse to connect")
    else:
        missing = ", ".join(n for n, v in ids.items() if not v)
        checks.warn(f"could not read a protocol id from {missing}: client '{cp}' hoprd '{hp}' (client {cv}, hoprd {hv}, server {sv})")
