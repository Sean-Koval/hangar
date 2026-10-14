"""gVisor checkpoint/restore timing: cold start of a warmed process vs restore, and one image restored twice (fork).

Spec 0012 §4.3. Needs root and runsc with its sidecar binaries (release 20261005 ships them in
gvisor.tar.zstd: put `runsc` and `gvisor-bin/` in a directory and set RUNSC to the runsc path). The sandbox's
root filesystem is the host's `/`, read-only, with a tmpfs /tmp, so no image has to be unpacked.

Usage: sudo RUNSC=/path/to/runsc python3 -I cr_bench.py <MB of heap>
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

EXP = Path(__file__).resolve().parent
RUNSC = os.environ.get("RUNSC", "runsc")
STATE = Path(os.environ.get("CR_STATE", "/tmp/cr_bench/state"))
MB = sys.argv[1] if len(sys.argv) > 1 else "256"
BASE = [RUNSC, "--root", str(STATE), "--network=none", "--ignore-cgroups"]


def sh(*args, check=True):
    if args[0] in ("run", "restore", "create"):
        # A detached sandbox inherits stdio; a pipe would never reach EOF.
        with open(STATE.parent / "sandbox.log", "a") as log:
            return subprocess.run([*BASE, *args], stdout=log, stderr=log, check=check, timeout=120)
    return subprocess.run([*BASE, *args], capture_output=True, text=True, check=check, timeout=120)


def beat(cid):
    r = sh("exec", cid, "cat", "/tmp/beat", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def wait_beat(cid, after=None, timeout=300):
    t = time.time()
    while time.time() - t < timeout:
        b = beat(cid)
        if b and b != after:
            return b
        time.sleep(0.05)
    raise TimeoutError(cid)


def cleanup(*ids):
    for i in ids:
        sh("delete", "-force", i, check=False)


def bundle(name):
    b = STATE.parent / f"bundle-{name}"
    subprocess.run([sys.executable, "-I", str(EXP / "mkbundle.py"), str(b), MB], check=True, capture_output=True)
    return b


out = {"heap_mb": int(MB)}
STATE.mkdir(parents=True, exist_ok=True)
ids = ["src", "f1", "f2"]
cleanup(*ids)
img = STATE.parent / "ckpt"
shutil.rmtree(img, ignore_errors=True)
img.mkdir()

t = time.time()
sh("run", "-detach", "-bundle", str(bundle("src")), "src")
b = wait_beat("src")
out["cold_start_to_ready_s"] = round(time.time() - t, 2)

t = time.time()
sh("checkpoint", "-image-path", str(img), "src")
out["checkpoint_s"] = round(time.time() - t, 2)
out["image_mb"] = round(sum(f.stat().st_size for f in img.rglob("*") if f.is_file()) / 2**20, 1)
cleanup("src")

for fid in ("f1", "f2"):
    bd = bundle(fid)
    t = time.time()
    sh("restore", "-detach", "-bundle", str(bd), "-image-path", str(img), fid)
    nb = wait_beat(fid, after=None)
    out[f"restore_{fid}_to_ready_s"] = round(time.time() - t, 2)
    out[f"restore_{fid}_beat"] = nb

# Both forks alive and progressing independently from the same image.
time.sleep(1)
out["forks_progressing"] = [beat("f1"), beat("f2")]
cleanup(*ids)
shutil.rmtree(img, ignore_errors=True)
print(json.dumps(out))
