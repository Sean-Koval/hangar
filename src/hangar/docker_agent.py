"""hangar-docker: starts Docker inside a heavy sandbox (--- heavy; docs/environments.md#the-heavy-tier).

Installed per sandbox at ~/.hangar/bin/hangar-docker (hangar.heavy) and run by hangar at claim, as root through sudo:

    sudo ~/.hangar/bin/hangar-docker start ~/.hangar/docker.json
    ~/.hangar/bin/hangar-docker status            # no sudo: asks the daemon over its socket

Standalone: the standard library of any python3 the image contract allows (3.8 and later), nothing from hangar. The
config file holds what hangar decided (the disk size, registry mirrors, the egress proxy); the last line `start`
prints is a JSON summary hangar records.

What it does, in order, and why:

* **Storage.** dockerd's overlay storage can't use virtio-fs as its upper layer, and under Kata the sandbox's volume
  for /var/lib/docker is virtio-fs (Kata's "How to run Docker in Docker with Kata Containers"). There it makes a sparse
  ext4 image on that volume and loop-mounts it on /var/lib/docker, the approach that guide recommends; on ext4, xfs,
  btrfs or tmpfs (a Docker volume, a microVM's root disk) it uses the directory as it is.
* **cgroups.** On cgroup v2, a container's processes sit in the root of its cgroup namespace, where no controller can
  be delegated to children; they move to an `init` child first (what moby's hack/dind does). Kata's guest defaults to
  cgroup v1, where nothing needs doing.
* **iptables.** dockerd's NAT and published ports need iptables; nft is tried first, then legacy.
* **MTU.** The bridge gets the sandbox interface's MTU: a 1500-byte bridge on a 1460-byte pod network drops large
  packets.
* **dockerd**, under a small loop that restarts it when it exits, with its socket owned by the sandbox user's group,
  so the agent runs `docker` without sudo. Nothing listens on TCP.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time

DATA = "/var/lib/docker"
SOCK = "/var/run/docker.sock"
DAEMON_JSON = "/etc/docker/daemon.json"
LOG = "/var/log/hangar-dockerd.log"
PIDFILE = "/run/hangar-dockerd.pid"
IMAGE = ".hangar-disk.img"
GOOD_FS = ("ext4", "ext3", "xfs", "btrfs", "tmpfs", "zfs")  # overlay works on these
SHARED_FS = ("virtiofs", "fuse", "fuseblk", "9p", "overlay", "nfs", "nfs4")  # it doesn't on these
START_WAIT = 90.0


# ====================================================================== small helpers
def ping(sock=SOCK, timeout=2.0):
    """True when dockerd answers on its socket (GET /_ping)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock)
        s.sendall(b"GET /_ping HTTP/1.0\r\nHost: docker\r\n\r\n")
        data = s.recv(4096)
        return data.split(b"\r\n", 1)[0].split()[1:2] == [b"200"]
    except OSError:
        return False
    finally:
        s.close()


def api(path, sock=SOCK, timeout=5.0):
    """GET a Docker API path over the socket; the decoded JSON body, or None."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock)
        s.sendall(f"GET {path} HTTP/1.0\r\nHost: docker\r\n\r\n".encode())
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 4 * 1024 * 1024:
                return None
        _, _, body = buf.partition(b"\r\n\r\n")
        return json.loads(body.decode() or "null")
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def fstype(path, mounts="/proc/mounts"):
    """The filesystem type of the mount `path` is on (the longest mount point that is a prefix of it)."""
    path = os.path.realpath(path)
    best, kind = "", ""
    try:
        with open(mounts) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                    best, kind = mnt, parts[2]
    except OSError:
        return ""
    return kind


def storage_plan(kind, disk_gb, free_bytes):
    """("direct", None) to use the directory as it is, or ("loop", size in bytes) for a loop-mounted ext4 image."""
    if kind in GOOD_FS or not kind:
        return "direct", None
    if kind in SHARED_FS or kind.startswith("fuse."):
        want = int(float(disk_gb or 30) * (1 << 30))
        room = max(0, free_bytes - (1 << 30))  # a GiB left for the volume's own use
        return "loop", max(1 << 30, min(want, room))
    return "direct", None


def daemon_config(conf, data_root, mtu):
    """/etc/docker/daemon.json from hangar's config."""
    out = {
        "data-root": data_root,
        "hosts": ["unix://" + SOCK],
        "group": conf.get("group") or conf.get("user") or "root",
        "log-driver": "json-file",
        "log-opts": {"max-size": "10m", "max-file": "3"},
    }
    mirrors = [m for m in conf.get("mirrors") or [] if isinstance(m, str) and m.startswith(("http://", "https://"))]
    if mirrors:
        out["registry-mirrors"] = mirrors
    if conf.get("insecure"):
        out["insecure-registries"] = list(conf["insecure"])
    if conf.get("proxy"):
        np = ",".join(conf.get("no_proxy") or ["localhost", "127.0.0.1"])
        out["proxies"] = {"http-proxy": conf["proxy"], "https-proxy": conf["proxy"], "no-proxy": np}
    if mtu and mtu < 1500:
        out["mtu"] = mtu
        out["default-network-opts"] = {"bridge": {"com.docker.network.driver.mtu": str(mtu)}}
    return out


def _run(argv, timeout=60):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def _nodes():
    """Kata's guest gives a privileged container no device nodes of the host, and only a few of its own: the loop
    devices are made here (CAP_MKNOD inside the VM)."""
    def node(path, kind, major, minor):
        if not os.path.exists(path):
            try:
                os.mknod(path, 0o660 | kind, os.makedev(major, minor))
            except OSError:
                pass
    node("/dev/loop-control", stat.S_IFCHR, 10, 237)
    for i in range(16):
        node(f"/dev/loop{i}", stat.S_IFBLK, 7, i)


def prepare_storage(conf):
    """(data root, how) for dockerd: the volume as it is, or an ext4 image loop-mounted on it."""
    os.makedirs(DATA, exist_ok=True)
    kind = fstype(DATA)
    st = os.statvfs(DATA)
    plan, size = storage_plan(kind, conf.get("disk_gb"), st.f_bavail * st.f_frsize)
    if plan == "direct":
        return DATA, f"{kind or 'unknown'} at {DATA}"
    img = os.path.join(DATA, IMAGE)
    fresh = not os.path.exists(img)
    if fresh:
        with open(img, "wb") as f:  # sparse: blocks are allocated as Docker writes them
            f.truncate(size)
        code, out = _run(["mkfs.ext4", "-q", "-F", "-m", "0", "-E", "lazy_itable_init=1,lazy_journal_init=1", img])
        if code != 0:
            raise RuntimeError(f"mkfs.ext4 failed: {out[-300:]}")
    _nodes()
    code, out = _run(["mount", "-o", "loop,noatime", img, DATA])
    if code != 0:
        raise RuntimeError(f"couldn't loop-mount {img} on {DATA} ({kind}): {out[-300:]}")
    return DATA, f"ext4 image of {size >> 30} GiB loop-mounted on {kind}"


def cgroup_nesting(root="/sys/fs/cgroup"):
    """cgroup v2: move this container's processes out of its namespace root and delegate the controllers."""
    if not os.path.exists(os.path.join(root, "cgroup.controllers")):
        return "v1"
    try:
        with open("/proc/self/cgroup") as f:
            if f.read().strip() != "0::/":
                return "v2"  # not the namespace root: delegation is the runtime's
        init = os.path.join(root, "init")
        os.makedirs(init, exist_ok=True)
        for _ in range(5):  # processes can appear while they move
            with open(os.path.join(root, "cgroup.procs")) as f:
                pids = [p for p in f.read().split() if p]
            if not pids:
                break
            for pid in pids:
                try:
                    with open(os.path.join(init, "cgroup.procs"), "w") as f:
                        f.write(pid)
                except OSError:
                    pass
        with open(os.path.join(root, "cgroup.controllers")) as f:
            ctrls = f.read().split()
        with open(os.path.join(root, "cgroup.subtree_control"), "w") as f:
            f.write(" ".join("+" + c for c in ctrls))
    except OSError as e:
        return f"v2 (nesting not set up: {e.strerror or e})"
    return "v2"


def iptables_mode():
    if _run(["iptables", "-nL"], 20)[0] == 0:
        return "nft" if "nf_tables" in _run(["iptables", "--version"], 10)[1] else "legacy"
    if shutil.which("iptables-legacy") and _run(["iptables-legacy", "-nL"], 20)[0] == 0:
        for t in ("iptables", "ip6tables"):
            _run(["update-alternatives", "--set", t, f"/usr/sbin/{t}-legacy"], 20)
        return "legacy"
    return "none"


def iface_mtu():
    """The MTU of the interface the default route uses (the pod's or the VM's)."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 1 and parts[1] == "00000000":
                    with open(f"/sys/class/net/{parts[0]}/mtu") as m:
                        return int(m.read().strip())
    except (OSError, ValueError):
        pass
    return None


def _alive(pidfile=PIDFILE):
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def loop(config_file):
    """dockerd, started again when it exits (backoff up to 30 s). SIGTERM stops both."""
    import signal

    child = {"p": None}

    def stop(*_):
        if child["p"] is not None and child["p"].poll() is None:
            child["p"].terminate()
            try:
                child["p"].wait(timeout=20)
            except subprocess.TimeoutExpired:
                child["p"].kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    delay = 1.0
    while True:
        t0 = time.time()
        child["p"] = subprocess.Popen(["dockerd", "--config-file", config_file])
        code = child["p"].wait()
        sys.stderr.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} hangar-docker: dockerd exited {code}\n")
        sys.stderr.flush()
        delay = 1.0 if time.time() - t0 > 60 else min(30.0, delay * 2)
        time.sleep(delay)


def spawn():
    """The restart loop, detached in its own session, its output in LOG."""
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    log = open(LOG, "ab")
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "loop", DAEMON_JSON], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, close_fds=True)
    with open(PIDFILE, "w") as f:
        f.write(str(p.pid))


def container_proxy(proxy, resolve=socket.gethostbyname):
    """The egress proxy's URL with its host as an address: containers don't have the sandbox's /etc/hosts entry."""
    from urllib.parse import urlsplit

    if not proxy:
        return None
    u = urlsplit(proxy)
    try:
        ip = resolve(u.hostname) if u.hostname else None
    except OSError:
        ip = None
    if not ip:
        return proxy
    auth = u.netloc.rpartition("@")[0]
    host = f"{auth}@{ip}" if auth else ip
    return f"{u.scheme}://{host}:{u.port}" if u.port else f"{u.scheme}://{host}"


def user_config(conf):
    """~/.docker/config.json for the sandbox user: in a proxied sandbox, containers and builds get the egress proxy
    (by address: they can't resolve hangar's names)."""
    home, user = conf.get("home"), conf.get("user")
    proxy = container_proxy(conf.get("proxy"))
    if not proxy or not home or not user:
        return
    d = os.path.join(home, ".docker")
    path = os.path.join(d, "config.json")
    try:
        with open(path) as f:
            cur = json.load(f)
        if not isinstance(cur, dict):
            cur = {}
    except (OSError, ValueError):
        cur = {}
    np = ",".join(conf.get("no_proxy") or ["localhost", "127.0.0.1"])
    cur["proxies"] = {"default": {"httpProxy": proxy, "httpsProxy": proxy, "noProxy": np}}
    os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(cur, f, indent=1)
    import pwd

    pw = pwd.getpwnam(user)
    for p in (d, path):
        os.chown(p, pw.pw_uid, pw.pw_gid)
    os.chmod(path, 0o600)


def summary(ok, **kw):
    print(json.dumps({"ok": ok, **kw}, sort_keys=True))
    return 0 if ok else 1


def start(conf_path):
    if os.geteuid() != 0:
        return summary(False, error="run it as root: sudo ~/.hangar/bin/hangar-docker start ~/.hangar/docker.json")
    try:
        with open(conf_path) as f:
            conf = json.load(f)
    except (OSError, ValueError) as e:
        return summary(False, error=f"can't read {conf_path}: {e}")
    if ping():
        info = api("/info") or {}
        return summary(True, already=True, version=info.get("ServerVersion"), driver=info.get("Driver"),
                       data_root=info.get("DockerRootDir"), cgroup=info.get("CgroupVersion"))
    if not shutil.which("dockerd"):
        return summary(False, error="this image has no dockerd: is the environment heavy (heavy: true)?")
    try:
        data_root, how = prepare_storage(conf)
    except (OSError, RuntimeError) as e:
        return summary(False, error=f"storage: {e}")
    cgroup = cgroup_nesting()
    ipt = iptables_mode()
    mtu = iface_mtu()
    os.makedirs(os.path.dirname(DAEMON_JSON), exist_ok=True)
    with open(DAEMON_JSON, "w") as f:
        json.dump(daemon_config(conf, data_root, mtu), f, indent=1)
    if not _alive():
        spawn()
    deadline = time.time() + START_WAIT
    while time.time() < deadline and not ping():
        time.sleep(0.5)
    if not ping():
        tail = ""
        try:
            with open(LOG, "rb") as f:
                f.seek(max(0, os.path.getsize(LOG) - 1500))
                tail = f.read().decode(errors="replace")
        except OSError:
            pass
        return summary(False, error=f"dockerd didn't answer in {int(START_WAIT)}s; its log ({LOG}) ends: "
                                    f"{tail.strip()[-800:]}", storage=how, cgroup=cgroup, iptables=ipt)
    try:
        user_config(conf)
    except (OSError, KeyError) as e:
        sys.stderr.write(f"hangar-docker: couldn't write the user's docker config: {e}\n")
    info = api("/info") or {}
    return summary(True, version=info.get("ServerVersion"), driver=info.get("Driver"),
                   data_root=info.get("DockerRootDir") or data_root, cgroup=info.get("CgroupVersion") or cgroup,
                   storage=how, iptables=ipt, mtu=mtu)


def status():
    if not ping():
        return summary(False, error=f"dockerd isn't answering on {SOCK}; start it with "
                                    "`sudo ~/.hangar/bin/hangar-docker start ~/.hangar/docker.json`")
    info = api("/info") or {}
    return summary(True, version=info.get("ServerVersion"), driver=info.get("Driver"),
                   containers=info.get("ContainersRunning"), images=info.get("Images"))


USAGE = """usage: hangar-docker start CONFIG | status

  start CONFIG   start dockerd as root (sudo), with the settings hangar wrote to CONFIG
  status         whether dockerd answers on /var/run/docker.sock
"""


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args.pop(0) if args else "help"
    if cmd == "start" and len(args) == 1:
        return start(args[0])
    if cmd == "status":
        return status()
    if cmd == "loop" and len(args) == 1:
        loop(args[0])
        return 0
    sys.stderr.write(USAGE)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())
