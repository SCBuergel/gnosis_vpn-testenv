"""T02-build-provenance: version strings and the compiled-in HOPR wire-protocol identifiers of client worker,
hoprd and server; asserts one protocol id across the stack. Writes provenance.json."""
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
    lcv = (shell.out([cfg.localcluster_bin, "--version"], timeout=30) or "").splitlines()[:1]
    d = {"client_version": cv, "client_protocol": cp, "hoprd_version": hv, "hoprd_protocol": hp, "server_version": sv, "server_protocol": sp,
         "client_image": ci, "server_image": si, "hoprd_sha256_16": _sha16(cfg.hoprd_bin),
         "localcluster_version": lcv[0] if lcv else "", "cluster_env": cfg.cluster_env, "cluster_latency": cfg.cluster_latency,
         "hoprd_bin": cfg.hoprd_bin, "client_image_tag": cfg.client_image, "cell": cfg.cell,
         "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    json.dump(d, open(run / "provenance.json", "w"), indent=1)
    print(json.dumps(d))
    checks.row(client=cv, client_protocol=cp, hoprd=hv, hoprd_protocol=hp, server=sv, server_protocol=sp)
    # a mismatch is the incompatibility this gate exists for; an unreadable id is a WARN because it cannot be judged
    ids = {"client": cp, "hoprd": hp, "server": sp}
    if all(ids.values()) and len(set(ids.values())) == 1:
        checks.passed(f"one protocol id {cp} across client {cv}, hoprd {hv}, server {sv}")
    elif all(ids.values()):
        checks.failed(f"protocol id mismatch: client {cv} speaks {cp}, hoprd {hv} speaks {hp}, server {sv} speaks {sp}; a HOPR "
                      f"packet's frame size is fixed, so mismatched versions misparse rather than refuse to connect")
    else:
        missing = ", ".join(n for n, v in ids.items() if not v)
        checks.warn(f"could not read a protocol id from {missing}: client '{cp}' hoprd '{hp}' server '{sp}' (client {cv}, hoprd {hv}, server {sv})")
