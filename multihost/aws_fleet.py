#!/usr/bin/env python3
"""An AWS EC2 fleet for the multi-machine testenv, the counterpart of do_fleet.py: one instance per client, per relay
and per exit, plus a control instance (the chain and the suite). Runs on the operator's machine; the keys never leave it.

    multihost/aws_python multihost/aws_fleet.py create  --name rs1 --clients 1 --exits 1 --size c7a.xlarge
    multihost/aws_python multihost/aws_fleet.py wait    --name rs1
    multihost/aws_python multihost/aws_fleet.py list    --name rs1
    multihost/aws_python multihost/aws_fleet.py resize  --name rs1 --member relay-1 --size c7a.2xlarge
    multihost/aws_python multihost/aws_fleet.py destroy --name rs1         # terminates every instance of the fleet, then checks
    multihost/aws_python multihost/aws_fleet.py bake    --ami gvpn-... --source root@cpu-8   # an AMI with everything

Credentials: the CSV AWS hands out for an access key (`Access key ID,Secret access key`), from AWS_KEYS_FILE (default
~/AWS_ACCESS_KEYS.csv), sent only to AWS. Nothing here prints them or passes them to an instance. Needs boto3
(multihost/aws_python runs a venv that has it).

Region: eu-north-1 by default, the one region this account's organisation policy (SCP) allows on 2026-10-08; every
other region answers UnauthorizedOperation with "explicit deny in a service control policy".

Machines: c7a/m7a (AMD EPYC, 7th generation) run one thread per core, so a vCPU is a physical core (DescribeInstanceTypes:
ThreadsPerCore 1). Not burstable: the cores are the instance's own. A fleet is found again by its Name tag prefix
(`gvpn-<name>-`) and the tag gvpn-fleet=<name>; ids are also kept in ~/.gvpn-fleets/aws-<name>.json.

Network: every instance in one subnet (one availability zone), in the security group gvpn-fleet: ssh from anywhere
(key only), everything between members of the group. The private addresses are what the stack binds to, as on DO.
An instance keeps its private address across stop/start, which `resize` uses: stop, change the type, start, and the
machine comes back on new hardware with its disk, its identity and its address.

Root login: the first-boot script puts the fleet key into root's authorized_keys (the Ubuntu AMI's root has a
"login as ubuntu" stub there), so every tool reaches every machine as root@, as on DigitalOcean."""
import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from do_fleet import READY, BAKED, STATE, ssh_argv, sh  # noqa: E402  (one fleet state dir, one ssh recipe)

REGION = os.environ.get("AWS_REGION_FLEET", "eu-north-1")
AZ = "eu-north-1a"
SG = "gvpn-fleet"
UBUNTU_OWNER = "099720109477"          # Canonical
UBUNTU_NAME = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
DISK_GB = 40
PROJECT_TAG = "gnosis-vpn-test-infra"
# The relay ladder: physical cores -> the smallest type with at least 4 GB (AMD 7th generation, no SMT)
LADDER = {1: "m7a.medium", 2: "c7a.large", 4: "c7a.xlarge", 8: "c7a.2xlarge", 16: "c7a.4xlarge", 32: "c7a.8xlarge", 64: "c7a.16xlarge"}
USER_DATA = """#!/bin/bash
set -x
install -d -m 700 /root/.ssh
echo '{pubkey}' > /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
if [ -f """ + BAKED + """ ]; then
  systemctl start docker; for i in $(seq 1 30); do docker version >/dev/null 2>&1 && break; sleep 1; done
  docker version >/dev/null && touch """ + READY + """
  exit 0
fi
export DEBIAN_FRONTEND=noninteractive
for i in $(seq 1 60); do apt-get update -qq && break; sleep 5; done
apt-get install -y -qq docker.io wireguard-tools gettext-base python3-pytest jq curl git iproute2 >/var/log/gvpn-apt.log 2>&1
systemctl enable --now docker
curl -fsSL --retry 5 -o /tmp/just.tgz https://github.com/casey/just/releases/download/1.58.0/just-1.58.0-x86_64-unknown-linux-musl.tar.gz
tar -xzf /tmp/just.tgz -C /tmp just && install -m 0755 /tmp/just /usr/local/bin/just
modprobe wireguard || true
docker version >/dev/null && command -v just && command -v wg && touch """ + READY + "\n"


def credentials():
    path = Path(os.environ.get("AWS_KEYS_FILE", Path.home() / "AWS_ACCESS_KEYS.csv")).expanduser()
    with open(path, encoding="utf-8-sig") as f:        # AWS writes the CSV with a byte-order mark and CRLF
        row = next(csv.DictReader(f))
    return {"aws_access_key_id": row["Access key ID"].strip(), "aws_secret_access_key": row["Secret access key"].strip()}


_session = None


def ec2():
    global _session
    if _session is None:
        _session = boto3.Session(**credentials(), region_name=REGION)
    return _session.client("ec2")


def prefix(name):
    return f"gvpn-{name}-"


def state_file(name):
    STATE.mkdir(exist_ok=True)
    return STATE / f"aws-{name}.json"


def norm(i):
    """One instance as the ladder sees a machine: the provider-neutral fields plus the raw record."""
    tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
    return {"id": i["InstanceId"], "name": tags.get("Name", ""), "status": i["State"]["Name"], "size": i["InstanceType"],
            "public": i.get("PublicIpAddress", ""), "private": i.get("PrivateIpAddress", ""), "az": i["Placement"]["AvailabilityZone"],
            "launched": str(i.get("LaunchTime", "")), "image": i.get("ImageId"), "cpu": i.get("CpuOptions"), "raw": i}


def fleet_instances(name, include_stopped=True):
    """Every instance of the fleet that is not terminated (stopped ones too: a resize stops one)."""
    states = ["pending", "running", "stopping", "stopped"] if include_stopped else ["pending", "running"]
    out = []
    for page in ec2().get_paginator("describe_instances").paginate(
            Filters=[{"Name": "tag:gvpn-fleet", "Values": [name]}, {"Name": "instance-state-name", "Values": states}]):
        for r in page["Reservations"]:
            out += [norm(i) for i in r["Instances"]]
    return out


def members(name):
    return {d["name"][len(prefix(name)):]: d for d in fleet_instances(name)}


def ubuntu_ami():
    imgs = ec2().describe_images(Owners=[UBUNTU_OWNER], Filters=[{"Name": "name", "Values": [UBUNTU_NAME]},
                                                                  {"Name": "state", "Values": ["available"]}])["Images"]
    return max(imgs, key=lambda i: i["CreationDate"])["ImageId"]


def resolve_image(image):
    if not image or image == "ubuntu":
        return ubuntu_ami()
    if image.startswith("ami-"):
        return image
    imgs = ec2().describe_images(Owners=["self"], Filters=[{"Name": "name", "Values": [image]}])["Images"]
    if not imgs:
        raise SystemExit(f"no AMI named {image!r} in {REGION}")
    return imgs[0]["ImageId"]


def subnet():
    s = ec2().describe_subnets(Filters=[{"Name": "availability-zone", "Values": [AZ]}, {"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
    if not s:
        raise SystemExit(f"no default subnet in {AZ}")
    return s[0]["SubnetId"], s[0]["VpcId"]


def security_group():
    c = ec2()
    sn, vpc = subnet()
    g = c.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG]}, {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
    if g:
        return g[0]["GroupId"]
    gid = c.create_security_group(GroupName=SG, Description="gnosis_vpn-testenv fleet: ssh, all traffic inside the group", VpcId=vpc)["GroupId"]
    c.authorize_security_group_ingress(GroupId=gid, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "ssh, key only"}]},
        {"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": gid, "Description": "inside the fleet"}]}])
    return gid


def launch(name, member, size, image, pubkey):
    """One instance gvpn-<name>-<member>; returns its id. A capacity or quota refusal comes back as SystemExit."""
    sn, _ = subnet()
    tags = [{"Key": "Name", "Value": prefix(name) + member}, {"Key": "gvpn-fleet", "Value": name}, {"Key": "project", "Value": PROJECT_TAG}]
    try:
        r = ec2().run_instances(
            ImageId=resolve_image(image), InstanceType=size, MinCount=1, MaxCount=1,
            NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": sn, "Groups": [security_group()], "AssociatePublicIpAddress": True}],
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": DISK_GB, "VolumeType": "gp3", "DeleteOnTermination": True}}],
            UserData=USER_DATA.format(pubkey=Path(pubkey).expanduser().read_text().strip()),
            TagSpecifications=[{"ResourceType": "instance", "Tags": tags}, {"ResourceType": "volume", "Tags": tags}],
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            InstanceInitiatedShutdownBehavior="stop")
    except ClientError as e:
        raise SystemExit(f"run_instances {member} ({size}): {e.response['Error']['Code']}: {e.response['Error']['Message'][:300]}")
    iid = r["Instances"][0]["InstanceId"]
    sf = state_file(name)
    st = json.loads(sf.read_text()) if sf.exists() else {"name": name, "ids": [], "region": REGION}
    st["ids"] = sorted(set(st["ids"]) | {iid})
    sf.write_text(json.dumps(st, indent=2))
    return iid


def create(a):
    if fleet_instances(a.name):
        raise SystemExit(f"a fleet '{a.name}' already exists; destroy it or pick another --name")
    groups = [(["control"], getattr(a, "control_size", None) or a.size)] + [([f"{role}-{i}" for i in range(1, n + 1)], a.size)
                                        for role, n in (("client", a.clients), ("relay", a.relays), ("exit", a.exits)) if n]
    for spec in a.add or []:
        group, size, count = spec.split(":")
        groups.append(([f"{group}-{i}" for i in range(1, int(count) + 1)], size))
    ids = []
    try:
        for ms, size in groups:
            for m in ms:
                ids.append(launch(a.name, m, size, a.image, a.pubkey))
    except SystemExit as e:
        print(f"create failed after {len(ids)} instance(s): {e}; terminating them", file=sys.stderr)
        destroy(argparse.Namespace(name=a.name))
        raise
    print(f"created {len(ids)} instances in {AZ}: " + "; ".join(f"{len(m)} x {s}: {', '.join(m)}" for m, s in groups))


def ready(ip, key):
    return subprocess.run(ssh_argv(key, f"root@{ip}") + [f"test -f {READY}"], capture_output=True, timeout=40).returncode == 0


def wait(a, only=None):
    """Every instance (or the members in `only`) running, with a public address, and its first boot done."""
    key = str(Path(a.key).expanduser())
    t0 = time.time()
    while True:
        ms = members(a.name)
        want = {k: d for k, d in ms.items() if only is None or k in only}
        pending = [k for k, d in want.items() if d["status"] != "running" or not d["public"]]
        if want and not pending and (only is None or set(only) <= set(want)):
            break
        if time.time() - t0 > a.timeout:
            raise SystemExit(f"not running after {a.timeout}s: {pending or sorted(set(only or ()) - set(want))}")
        time.sleep(5)
    print(f"{len(want)} running after {int(time.time() - t0)}s; waiting for first-boot setup", flush=True)
    todo = {k: d["public"] for k, d in want.items()}
    while todo:
        for k, ip in list(todo.items()):
            if ready(ip, key):
                del todo[k]
        if todo and time.time() - t0 > a.timeout:
            raise SystemExit(f"first-boot setup not done after {a.timeout}s: {sorted(todo)}")
        if todo:
            time.sleep(10)
    print(f"fleet '{a.name}' ready after {int(time.time() - t0)}s", flush=True)
    return members(a.name)


def terminate(ids):
    ids = list(ids)
    if not ids:
        return
    try:
        ec2().terminate_instances(InstanceIds=ids)
    except ClientError as e:
        if "InvalidInstanceID.NotFound" not in str(e):
            raise


def destroy(a):
    ids = set()
    sf = state_file(a.name)
    if sf.exists():
        ids |= set(json.loads(sf.read_text())["ids"])
    ids |= {d["id"] for d in fleet_instances(a.name)}
    live = {i["InstanceId"] for r in ec2().describe_instances(InstanceIds=sorted(ids))["Reservations"] for i in r["Instances"]
            if i["State"]["Name"] != "terminated"} if ids else set()
    # an id from the state file that AWS no longer knows makes describe fail as a whole: ask one at a time then
    terminate(live)
    t0 = time.time()
    while True:
        left = [d["name"] for d in fleet_instances(a.name)] + [
            i["InstanceId"] for r in (ec2().describe_instances(InstanceIds=sorted(live))["Reservations"] if live else [])
            for i in r["Instances"] if i["State"]["Name"] != "terminated"]
        if not left:
            print(f"fleet '{a.name}': {len(live)} instance(s) terminated, none left")
            sf.unlink(missing_ok=True)
            return
        if time.time() - t0 > 900:
            raise SystemExit(f"still there after 900 s: {left} - terminate them in the EC2 console ({REGION})")
        time.sleep(10)


def resize(a):
    """Stop the member, change its instance type, start it: new hardware, same disk, same private address."""
    d = members(a.name)[a.member]
    c = ec2()
    t0 = time.time()
    if d["status"] != "stopped":
        c.stop_instances(InstanceIds=[d["id"]])
        c.get_waiter("instance_stopped").wait(InstanceIds=[d["id"]], WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    c.modify_instance_attribute(InstanceId=d["id"], InstanceType={"Value": a.size})
    c.start_instances(InstanceIds=[d["id"]])
    c.get_waiter("instance_running").wait(InstanceIds=[d["id"]], WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    n = members(a.name)[a.member]
    print(f"{a.member}: {d['size']} -> {n['size']} in {int(time.time() - t0)} s; private {d['private']} -> {n['private']}, "
          f"public {d['public']} -> {n['public']}")
    return n


def show(a):
    for k, d in sorted(members(a.name).items()):
        print(d["id"], d["name"], d["status"], d["size"], d["az"], d["public"], d["private"])


def bake(a):
    """An AMI with everything a fleet machine needs (the first-boot tools, the images, the node binaries, the repo), so
    machines made from it skip installs and provisioning. Built on a throwaway instance, terminated afterwards."""
    import shlex
    name = f"bake-{int(time.time())}"
    launch(name, "builder", a.size, "ubuntu", a.key + ".pub")
    try:
        ms = wait(argparse.Namespace(name=name, key=a.key, timeout=900))
        d = ms["builder"]
        ip = d["public"]
        dst = " ".join(shlex.quote(x) for x in ssh_argv(a.key, f"root@{ip}"))
        src = " ".join(shlex.quote(x) for x in ssh_argv(a.source_key, a.source, known=str(Path.home() / ".ssh" / "known_hosts")))
        print(f"builder {d['id']} {ip}: loading images from {a.source}", flush=True)
        sh(f"{src} {shlex.quote('docker save ' + ' '.join(a.images) + ' | gzip -1')} | {dst} 'gunzip | docker load'", timeout=3600)
        print("node binaries", flush=True)
        sh(f"{dst} 'mkdir -p /root/relayscale/bin && cd /root/relayscale/bin && curl -fsSL --retry 3 -o hoprd-4.1.3-release "
           f"{a.hoprd_url} && chmod +x hoprd-4.1.3-release'")
        got = sh(f"{dst} 'sha256sum /root/relayscale/bin/hoprd-4.1.3-release'").split()[0]
        if got != a.hoprd_sha256:
            raise SystemExit(f"hoprd checksum mismatch: {got} != {a.hoprd_sha256}")
        sh(f"{src} {shlex.quote('cat ' + a.localcluster)} | {dst} 'cat > /root/relayscale/bin/hoprd-localcluster && chmod +x /root/relayscale/bin/hoprd-localcluster'")
        want = sh(f"{src} {shlex.quote('sha256sum ' + a.localcluster)}").split()[0]
        got = sh(f"{dst} 'sha256sum /root/relayscale/bin/hoprd-localcluster'").split()[0]
        if want != got:
            raise SystemExit(f"localcluster checksum mismatch: {want} != {got}")
        print("chain image", flush=True)
        sh(f"{dst} 'docker pull -q {a.chain_image}@{a.chain_digest} >/dev/null && docker tag {a.chain_image}@{a.chain_digest} {a.chain_image}:latest'", timeout=1800)
        manifest = (f"gvpn AMI {a.ami} built {time.strftime('%FT%TZ', time.gmtime())}; images: {' '.join(a.images)} {a.chain_image}@{a.chain_digest}; "
                    f"hoprd sha256 {a.hoprd_sha256}; localcluster sha256 {want}")
        sh(f"{dst} {shlex.quote(f'echo {shlex.quote(manifest)} > {BAKED} && rm -f {READY} && : > /root/.ssh/authorized_keys && apt-get clean && journalctl --vacuum-size=1M >/dev/null 2>&1; cloud-init clean --logs --seed; sync')}")
        print("creating the AMI (several minutes)", flush=True)
        c = ec2()
        img = c.create_image(InstanceId=d["id"], Name=a.ami, Description=manifest[:255],
                             TagSpecifications=[{"ResourceType": "image", "Tags": [{"Key": "project", "Value": PROJECT_TAG}]}])["ImageId"]
        c.get_waiter("image_available").wait(ImageIds=[img], WaiterConfig={"Delay": 15, "MaxAttempts": 240})
        print(f"AMI {a.ami} = {img}; use it: aws_fleet.py create --image {img} ...")
    finally:
        destroy(argparse.Namespace(name=name))


QUOTA = "L-1216C47A"        # Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances, in vCPUs


def quota(a):
    """The on-demand vCPU quota, the vCPUs in use, and the increase requests; --request N asks for N."""
    ec2()
    sq = _session.client("service-quotas")
    q = sq.get_service_quota(ServiceCode="ec2", QuotaCode=QUOTA)["Quota"]["Value"]
    used = sum(i["CpuOptions"]["CoreCount"] * i["CpuOptions"]["ThreadsPerCore"] for r in ec2().describe_instances(
        Filters=[{"Name": "instance-state-name", "Values": ["pending", "running"]}])["Reservations"] for i in r["Instances"])
    print(f"{REGION}: on-demand standard vCPU quota {q:.0f}, in use {used}")
    for r in sq.list_requested_service_quota_change_history_by_quota(ServiceCode="ec2", QuotaCode=QUOTA)["RequestedQuotas"]:
        print(f"  request {r['Id']}: {r['DesiredValue']:.0f} vCPUs, {r['Status']}, {r['Created']:%F %H:%M}")
    if a.request:
        r = sq.request_service_quota_increase(ServiceCode="ec2", QuotaCode=QUOTA, DesiredValue=float(a.request))["RequestedQuota"]
        print(f"  requested {a.request}: {r['Status']}")


def amis(a):
    for i in ec2().describe_images(Owners=["self"])["Images"]:
        print(i["ImageId"], i["Name"], i["State"], i["CreationDate"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("bake", help="build an AMI with everything a fleet machine needs")
    k.add_argument("--ami", required=True, help="AMI name, e.g. gvpn-4.1.3-0.96.4-2026-10-08")
    k.add_argument("--size", default="c7a.xlarge")
    k.add_argument("--key", default="~/.ssh/do_testenv_fleet_ed25519")
    k.add_argument("--source", required=True, help="ssh target holding the images and the localcluster binary")
    k.add_argument("--source-key", default="~/.ssh/do_probe_ed25519")
    k.add_argument("--images", nargs="+", default=["gnosis_vpn-client:0.96.4-snap-20260929.073145", "gnosis_vpn-suite-tools:latest",
                                                   "gnosis_vpn-server:0.7.0-upstream", "gnosis_vpn-target:latest"])
    k.add_argument("--localcluster", default="/root/testenv/hoprd/target/release/hoprd-localcluster")
    k.add_argument("--hoprd-url", default="https://github.com/hoprnet/hoprd/releases/download/v4.1.3/hoprd-x86_64-linux")
    k.add_argument("--hoprd-sha256", default="28e00b385a4630ccbdab88cbcf59ed266d47141f38f97891732925312e40339d")
    k.add_argument("--chain-image", default="europe-west3-docker.pkg.dev/hoprassociation/docker-images/bloklid-anvil")
    k.add_argument("--chain-digest", default="sha256:c901116c499836aebb22c22dc69e9c235d408a8446e00e0b62841a1db79f70ad")
    sub.add_parser("amis", help="list this account's AMIs")
    q = sub.add_parser("quota", help="the on-demand vCPU quota, what is in use, increase requests")
    q.add_argument("--request", type=int, help="ask for this many vCPUs")
    for c in ("create", "wait", "destroy", "list", "resize"):
        p = sub.add_parser(c)
        p.add_argument("--name", required=True, help="fleet name; instances are gvpn-<name>-<role>-<i>")
        if c == "create":
            p.add_argument("--clients", type=int, default=1)
            p.add_argument("--relays", type=int, default=0)
            p.add_argument("--exits", type=int, default=1)
            p.add_argument("--size", default="c7a.xlarge")
            p.add_argument("--image", default="ubuntu", help="'ubuntu' (24.04), an AMI id or the name of one of this account's AMIs")
            p.add_argument("--pubkey", default="~/.ssh/do_testenv_fleet_ed25519.pub")
            p.add_argument("--add", action="append", metavar="GROUP:SIZE:COUNT", help="an extra group with its own type, e.g. relay:m7a.medium:1")
        if c == "wait":
            p.add_argument("--key", default="~/.ssh/do_testenv_fleet_ed25519")
            p.add_argument("--timeout", type=int, default=900)
        if c == "resize":
            p.add_argument("--member", required=True, help="e.g. relay-1")
            p.add_argument("--size", required=True)
    a = ap.parse_args()
    {"create": create, "wait": wait, "destroy": destroy, "list": show, "resize": resize, "bake": bake, "amis": amis, "quota": quota}[a.cmd](a)


if __name__ == "__main__":
    main()
