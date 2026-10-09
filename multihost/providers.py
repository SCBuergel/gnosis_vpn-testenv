"""The machine providers the ladder runs on, behind one interface: DigitalOcean (do_fleet.py) and AWS EC2 (aws_fleet.py).

A machine is a dict {"id", "name", "status" ("running" when usable), "size", "public", "private", "raw"}; a fleet's
machines are keyed by their member name (control, client-1, exit-1, relay-c4, ...)."""
import argparse
import json
import time
from pathlib import Path


class DigitalOcean:
    name = "digitalocean"
    relay_image_default = "ubuntu-24-04-x64"     # a small size's disk is below a snapshot's minimum: relays boot plain
    can_resize = False

    def __init__(self):
        import do_fleet
        self.f = do_fleet

    def _norm(self, d):
        pub, priv = self.f.addrs(d)
        return {"id": d["id"], "name": d["name"], "status": "running" if d["status"] == "active" else d["status"],
                "size": d["size_slug"], "public": pub, "private": priv, "raw": d}

    def machines(self, fleet):
        return {d["name"][len(self.f.prefix(fleet)):]: self._norm(d) for d in self.f.fleet_droplets(fleet)}

    def create_fleet(self, fleet, clients, exits, size, image, pubkey, project=None, region=None, control_size=None):
        if control_size and control_size != size:
            raise SystemExit("DigitalOcean: control has the clients' and exits' size here; drop --control-size")
        self.f.create(argparse.Namespace(name=fleet, clients=clients, relays=0, exits=exits, region=region, size=size,
                                         image=str(image), pubkey=pubkey, add=None, project=project or self.f.PROJECT))

    def wait(self, fleet, key, timeout=1500):
        self.f.wait(argparse.Namespace(name=fleet, key=key, timeout=timeout))

    def create_one(self, fleet, member, size, image, pubkey, project=None, region=None):
        body = {"names": [self.f.prefix(fleet) + member], "region": region, "size": size, "image": image, "ipv6": False,
                "monitoring": False, "user_data": self.f.USER_DATA.format(pubkey=Path(pubkey).expanduser().read_text().strip())}
        d = self.f.api("POST", "/droplets", body)["droplets"][0]
        self.f.assign_to_project(project or self.f.PROJECT, [d["id"]])
        return d["id"]

    def delete(self, mid):
        try:
            self.f.api("DELETE", f"/droplets/{mid}")
        except SystemExit as e:
            if "404" not in str(e):
                raise

    def destroy(self, fleet):
        self.f.destroy(argparse.Namespace(name=fleet))

    def record(self, m):
        d = m["raw"]
        api = {k: d.get(k) for k in ("id", "name", "size_slug", "vcpus", "memory", "disk", "created_at", "image", "kernel", "features")}
        api["region"] = d["region"]["slug"]
        api["size"] = d.get("size")
        return api

    def resize(self, fleet, member, size):
        raise SystemExit("DigitalOcean: no in-place resize here (a droplet resize keeps the disk size class); use --swap new")


class AWS:
    name = "aws"
    relay_image_default = None                    # the base AMI: EBS size does not depend on the type
    can_resize = True

    def __init__(self):
        import aws_fleet
        self.f = aws_fleet

    def machines(self, fleet):
        return self.f.members(fleet)

    def create_fleet(self, fleet, clients, exits, size, image, pubkey, project=None, region=None, control_size=None):
        self.f.create(argparse.Namespace(name=fleet, clients=clients, relays=0, exits=exits, size=size, image=str(image),
                                         pubkey=pubkey, add=None, control_size=control_size))

    def wait(self, fleet, key, timeout=1500, only=None):
        self.f.wait(argparse.Namespace(name=fleet, key=key, timeout=timeout), only=only)

    def create_one(self, fleet, member, size, image, pubkey, project=None, region=None):
        return self.f.launch(fleet, member, size, image, pubkey)

    def delete(self, mid):
        self.f.terminate([mid])

    def destroy(self, fleet):
        self.f.destroy(argparse.Namespace(name=fleet))

    def record(self, m):
        i = m["raw"]
        rec = {k: i.get(k) for k in ("InstanceId", "InstanceType", "ImageId", "CpuOptions", "Placement", "Architecture",
                                     "PrivateIpAddress", "PublicIpAddress", "Hypervisor", "PlatformDetails")}
        rec["LaunchTime"] = str(i.get("LaunchTime"))
        try:
            t = self.f.ec2().describe_instance_types(InstanceTypes=[i["InstanceType"]])["InstanceTypes"][0]
            rec["type"] = {"vcpu": t["VCpuInfo"], "memory_mib": t["MemoryInfo"]["SizeInMiB"], "processor": t["ProcessorInfo"],
                           "network": t["NetworkInfo"].get("NetworkPerformance"), "burstable": t.get("BurstablePerformanceSupported")}
        except Exception as e:      # noqa: BLE001
            rec["type"] = f"describe_instance_types failed: {e}"
        return json.loads(json.dumps(rec, default=str))

    def resize(self, fleet, member, size):
        return self.f.resize(argparse.Namespace(name=fleet, member=member, size=size))


def get(name):
    return {"digitalocean": DigitalOcean, "aws": AWS}[name]()


def ready_wait(prov, fleet, member, key, ready_fn, timeout=1500):
    """Poll until the member is running with a public address and ready_fn(public) is true; returns the machine."""
    t0 = time.time()
    while True:
        m = prov.machines(fleet).get(member)
        if m and m["status"] == "running" and m["public"] and ready_fn(m["public"]):
            return m, int(time.time() - t0)
        if time.time() - t0 > timeout:
            raise RuntimeError(f"{member} not ready after {timeout} s")
        time.sleep(10)
