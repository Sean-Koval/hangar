# Simulates a warmed process (language server / test runner with loaded state):
# imports modules, builds MB of heap, optional extra warmup, then heartbeats to /tmp/beat.
import hashlib
import json
import os
import sys
import time

t0 = time.time()
import argparse, asyncio, decimal, difflib, email, http.client, sqlite3, unittest, xml.etree.ElementTree  # noqa: E401,F401

MB = int(sys.argv[1]) if len(sys.argv) > 1 else 256
blob = [hashlib.sha256(str(i).encode()).hexdigest() * 16 for i in range(MB * 1024)]  # ~1 KiB each
time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 0)
print(json.dumps({"warm_s": round(time.time() - t0, 2), "items": len(blob), "pid": os.getpid()}), flush=True)
n = 0
while True:
    n += 1
    with open("/tmp/beat", "w") as f:
        f.write(f"{n} {len(blob)}\n")
    time.sleep(0.2)
