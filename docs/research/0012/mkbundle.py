"""Write an OCI config.json for runsc: host / as read-only rootfs, the experiment dir at /work."""
import json
import os
import subprocess
import sys
from pathlib import Path

exp = Path(__file__).resolve().parent
bundle = Path(sys.argv[1])
mb = sys.argv[2] if len(sys.argv) > 2 else "256"
bundle.mkdir(parents=True, exist_ok=True)
(bundle / "config.json").unlink(missing_ok=True)
subprocess.run([os.environ.get("RUNSC", "runsc"), "spec", "--bundle", str(bundle), "--", "/usr/bin/python3", "/work/warm.py", mb, "0"], check=True)
p = bundle / "config.json"
c = json.loads(p.read_text())
c["root"] = {"path": "/", "readonly": True}
c["process"]["terminal"] = False
c["mounts"].append({"destination": "/tmp", "type": "tmpfs", "source": "tmpfs", "options": ["nosuid", "nodev"]})
c["mounts"].append({"destination": "/work", "type": "bind", "source": str(exp), "options": ["rbind", "ro"]})
p.write_text(json.dumps(c, indent=1))
print("ok", p)
