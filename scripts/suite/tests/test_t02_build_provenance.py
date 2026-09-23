"""T02-build-provenance (gate): what is running, and can the pieces talk to each other? Records the version
strings and image digests of the client, hoprd and the exit server, the OCI revision label where an image carries
one, and the compiled-in HOPR wire-protocol id (/hopr/mix/<ver>) of the client worker and hoprd. Writes
provenance.json for the run.

Pass iff both protocol ids are readable and equal; FAIL when both are present and differ; WARN when either cannot be
read. The exit server drives its hoprd node over REST and embeds no id (its binary holds no /hopr/ string), so its
version is recorded and it joins the comparison only if a future build carries one.

Why: a release's lockfile once claimed a library version, from another branch with a wire-format change and a
bumped protocol id, that was not in the shipped binary. A HOPR packet's frame size does not depend on its content,
so mismatched versions misparse instead of failing to connect; reading the ids out of the binaries settles it."""
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
    # an image built with --label org.opencontainers.image.revision=<commit> names its source commit here
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
