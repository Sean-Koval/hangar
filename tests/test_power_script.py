"""deploy/gke/power.sh `up` against a stand-in gcloud: which autoscaling range each node pool comes back with.

--- review: docs/team-scale.md has operators apply terraform (a new machine type, sandbox_max_nodes 4 -> 10) while
the cluster is powered down. `down` saves each pool's range in a cluster label and turns autoscaling off; the apply
turns it back on with the new range; `up` then put the range saved at `down` back, so the next morning's power-up
quietly undid the apply.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("bash") or not shutil.which("python3"), reason="needs bash")

GCLOUD = r"""#!/bin/bash
echo "$*" >> "$GCLOUD_LOG"
case "$*" in
  *"node-pools list"*) printf 'system\nsandboxes-gvisor\n' ;;
  *"clusters describe"*) printf '%s\n' "$GCLOUD_LABELS" ;;
  *"node-pools describe system"*) printf 'False\t1\t2\n' ;;
  *"node-pools describe sandboxes-gvisor"*) printf '%b\n' "$GCLOUD_SANDBOX_AUTOSCALING" ;;
esac
"""


def power_up(tmp_path: Path, sandbox_autoscaling: str) -> list[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(GCLOUD)
    (bin_dir / "kubectl").write_text("#!/bin/bash\nexit 1\n")  # no cluster to talk to
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    log = tmp_path / "gcloud.log"
    labels = {"resourceLabels": {"hangar-power-system": "1-2", "hangar-power-sandboxes-gvisor": "0-4"}}
    env = {k: v for k, v in os.environ.items() if k != "HANGAR_POWER_INSTALL"}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", PROJECT="p", ZONE="z", CLUSTER="c", GCLOUD_LOG=str(log),
               GCLOUD_LABELS=json.dumps(labels), GCLOUD_SANDBOX_AUTOSCALING=sandbox_autoscaling)
    r = subprocess.run(["bash", str(ROOT / "deploy/gke/power.sh"), "up"], env=env, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return log.read_text().splitlines()


def updates(calls: list[str], pool: str) -> list[str]:
    return [c for c in calls if c.startswith("container clusters update") and f"--node-pool {pool}" in c]


def test_up_restores_the_range_saved_at_down(tmp_path):
    calls = power_up(tmp_path, r"False\t0\t4")
    assert any("--min-nodes 0 --max-nodes 4" in c for c in updates(calls, "sandboxes-gvisor"))
    assert any("--min-nodes 1 --max-nodes 2" in c for c in updates(calls, "system"))
    assert any("resize c" in c and "--node-pool system --num-nodes 1" in c for c in calls)


def test_up_keeps_a_range_set_while_the_cluster_was_down(tmp_path):
    """terraform apply in the power-down window: autoscaling is on again, 0-10. `up` keeps it and drops the label
    saved at `down`, instead of setting the pool back to 0-4."""
    calls = power_up(tmp_path, r"True\t0\t10")
    assert not any("--max-nodes 4" in c for c in updates(calls, "sandboxes-gvisor")), calls
    assert any("--remove-labels=hangar-power-sandboxes-gvisor" in c for c in calls), calls
    assert any("--min-nodes 1 --max-nodes 2" in c for c in updates(calls, "system"))  # the other pool as before


# ====================================================================== --- ops: only sandbox pools; hangar stays up
# A stand-in gcloud with state (pools, sizes and the cluster's labels persist between calls), and a kubectl that
# answers like a running hangar, so `down` then `up` can run in sequence.
GCLOUD_STATEFUL = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
line = " ".join(a)
with open(os.environ["GCLOUD_LOG"], "a") as f:
    f.write(line + "\n")
path = os.environ["GCLOUD_STATE"]
st = json.load(open(path))
def arg(name):
    return a[a.index(name) + 1]
def opt(prefix):
    return next(x.split("=", 1)[1] for x in a if x.startswith(prefix))
if "node-pools list" in line:
    print("\n".join(st["pools"]))
elif "node-pools describe" in line and "instanceGroupUrls" in line:
    print(f"https://www.googleapis.com/compute/v1/projects/p/zones/z/instanceGroupManagers/ig-{a[3]}")
elif "node-pools describe" in line:
    p = st["pools"][a[3]]
    print("True\t%s\t%s" % (p["min"], p["max"]) if p["on"] else "False\t\t")
elif "instance-groups managed describe" in line:
    print(st["pools"][a[4].rsplit("ig-", 1)[1]]["size"])
elif "clusters describe" in line:
    print(json.dumps({"resourceLabels": st["labels"]}))
elif "clusters update" in line and "--update-labels" in line:
    k, v = opt("--update-labels=").split("=")
    st["labels"][k] = v
elif "clusters update" in line and "--remove-labels" in line:
    st["labels"].pop(opt("--remove-labels="), None)
elif "clusters update" in line and "--no-enable-autoscaling" in line:
    st["pools"][arg("--node-pool")]["on"] = False
elif "clusters update" in line and "--enable-autoscaling" in line:
    st["pools"][arg("--node-pool")].update(on=True, min=int(arg("--min-nodes")), max=int(arg("--max-nodes")))
elif "clusters resize" in line:
    st["pools"][arg("--node-pool")]["size"] = int(arg("--num-nodes"))
json.dump(st, open(path, "w"))
"""

KUBECTL = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
line = " ".join(a)
log = open(os.environ["KUBECTL_LOG"], "a")
if "exec" in a:
    sys.stdin.read()
    rest = a[a.index("-") + 1:]
    log.write(json.dumps(["exec", *rest]) + "\n")
    if rest[:1] in (["0"], ["1"]):
        print("no sessions running")
    elif "/api/admin/power/prepull" in " ".join(rest):
        print(json.dumps({"images": ["r/env@sha256:1", "r/pack@sha256:2"], "missing": [],
                          "manifest": {"kind": "DaemonSet", "metadata": {"name": "hangar-prepull"}}}))
    elif "/api/admin/power/state" in " ".join(rest):
        print(json.dumps({"state": {}, "released_jobs": ["job-1"], "warm_pools": {"py": 2}}))
    sys.exit(0)
log.write(line + "\n")
if "readyReplicas" in line or ".spec.replicas" in line or "numberReady" in line:
    print("1", end="")
elif "get nodes" in line:
    print("gke-n1   Ready   <none>   1m   v1.35")
elif a[:2] == ["apply", "-f"]:
    open(os.environ["KUBECTL_APPLIED"], "w").write(sys.stdin.read())
"""


@pytest.fixture
def cluster(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(GCLOUD_STATEFUL)
    (bin_dir / "kubectl").write_text(KUBECTL)
    (bin_dir / "gke-gcloud-auth-plugin").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"labels": {}, "pools": {
        "system": {"on": True, "min": 1, "max": 2, "size": 1},
        "sandboxes-gvisor": {"on": True, "min": 1, "max": 10, "size": 3},
        "sandboxes-microvm": {"on": False, "min": None, "max": None, "size": 2}}}))
    env = {k: v for k, v in os.environ.items() if k != "HANGAR_POWER_INSTALL"}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", PROJECT="p", ZONE="z", CLUSTER="c",
               GCLOUD_LOG=str(tmp_path / "gcloud.log"), GCLOUD_STATE=str(state),
               KUBECTL_LOG=str(tmp_path / "kubectl.log"), KUBECTL_APPLIED=str(tmp_path / "applied.json"),
               POWER_UP_CRON="50 9 * * *", POWER_TIME_ZONE="America/New_York", POWER_WARM_MINUTES="10")

    def run(action: str) -> str:
        r = subprocess.run(["bash", str(ROOT / "deploy/gke/power.sh"), action], env=env, capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    def kubectl() -> list:
        log = tmp_path / "kubectl.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [json.loads(x) if x.startswith("[") else x for x in lines]

    return {"run": run, "state": lambda: json.loads(state.read_text()), "kubectl": kubectl, "tmp": tmp_path}


def _told(calls: list) -> list[dict]:
    return [json.loads(c[-1]) for c in calls if isinstance(c, list) and "/api/admin/power/state" in c]


def test_down_powers_only_the_sandbox_pools_and_leaves_hangar_up(cluster):
    cluster["run"]("down")
    st = cluster["state"]()
    assert st["pools"]["system"] == {"on": True, "min": 1, "max": 2, "size": 1}  # untouched
    assert st["pools"]["sandboxes-gvisor"]["size"] == 0 and not st["pools"]["sandboxes-gvisor"]["on"]
    assert st["pools"]["sandboxes-microvm"]["size"] == 0
    assert st["labels"] == {"hangar-power-sandboxes-gvisor": "1-10", "hangar-power-sandboxes-microvm": "fixed-2"}
    calls = cluster["kubectl"]()
    assert not any(isinstance(c, str) and "scale" in c for c in calls)  # on main: hangar scaled to 0
    assert _told(calls) == [{"state": "down", "up_cron": "50 9 * * *", "time_zone": "America/New_York",
                             "warm_minutes": "10"}]
    told_at = next(i for i, c in enumerate(calls) if isinstance(c, list) and "/api/admin/power/state" in c)
    assert told_at < calls.index(["exec", "1"])  # told before the drain, which then ends itself
    # A second `down` (a retry) keeps what the first saved instead of saving 0 nodes over it.
    cluster["run"]("down")
    assert cluster["state"]()["labels"] == st["labels"]


def test_up_restores_each_pool_prepulls_then_lets_jobs_start(cluster):
    cluster["run"]("down")
    out = cluster["run"]("up")
    st = cluster["state"]()
    assert st["pools"]["sandboxes-gvisor"] == {"on": True, "min": 1, "max": 10, "size": 1}  # the warm floor
    # the fixed-size microVM pool comes back at its size, without autoscaling (on main: autoscaling 0-4)
    assert st["pools"]["sandboxes-microvm"] == {"on": False, "min": None, "max": None, "size": 2}
    assert st["labels"] == {} and st["pools"]["system"]["size"] == 1
    calls = cluster["kubectl"]()
    states = _told(calls)[1:]
    assert [s["state"] for s in states] == ["starting", "up"]
    assert json.loads((cluster["tmp"] / "applied.json").read_text())["kind"] == "DaemonSet"
    i_nodes = next(i for i, c in enumerate(calls) if isinstance(c, str) and c.startswith("get nodes"))
    i_apply = next(i for i, c in enumerate(calls) if isinstance(c, str) and c.startswith("apply -f"))
    i_up = max(i for i, c in enumerate(calls) if isinstance(c, list) and "/api/admin/power/state" in c)
    assert i_nodes < i_apply < i_up  # the floor node first, then the prepull, then jobs
    assert states[1]["prepull"]["images"] == 2 and states[1]["prepull"]["ready"] is True
    assert "queued jobs starting: 1" in out and "warm pools for the day: py=2" in out
