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
