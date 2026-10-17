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
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("bash") or not shutil.which("python3"), reason="needs bash")

GCLOUD = r"""#!/bin/bash
echo "$*" >> "$GCLOUD_LOG"
case "$*" in
  *"--update-labels=hangar-power-lock="*) l="$*"; l=${l##*--update-labels=hangar-power-lock=}
                                         echo "${l%% *}" > "$GCLOUD_LOG.lock" ;;
  *"--remove-labels=hangar-power-lock"*) rm -f "$GCLOUD_LOG.lock" ;;
  *"node-pools list"*) printf 'system\nsandboxes-gvisor\n' ;;
  *"clusters describe"*) python3 -c 'import json, os
d, p = json.loads(os.environ["GCLOUD_LABELS"]), os.environ["GCLOUD_LOG"] + ".lock"
if os.path.exists(p):
    d["resourceLabels"]["hangar-power-lock"] = open(p).read().strip()
print(json.dumps(d))' ;;
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
with open(os.environ["CALL_LOG"], "a") as f:  # --- infra-state: gcloud and kubectl in one sequence
    f.write("gcloud " + line + "\n")
if os.environ.get("GCLOUD_FAIL") and os.environ["GCLOUD_FAIL"] in line:  # a call that fails, for the tests
    sys.stderr.write("ERROR: (gcloud) injected failure\n")
    sys.exit(1)
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
    for kv in opt("--update-labels=").split(","):
        k, v = kv.split("=")
        st["labels"][k] = v
elif "clusters update" in line and "--remove-labels" in line:
    for k in opt("--remove-labels=").split(","):
        st["labels"].pop(k, None)
elif "clusters update" in line and "--no-enable-autoscaling" in line:
    st["pools"][arg("--node-pool")]["on"] = False
elif "clusters update" in line and "--enable-autoscaling" in line:
    st["pools"][arg("--node-pool")].update(on=True, min=int(arg("--min-nodes")), max=int(arg("--max-nodes")))
elif "clusters resize" in line:
    st["pools"][arg("--node-pool")]["size"] = int(arg("--num-nodes"))
json.dump(st, open(path, "w"))
"""

# A kubectl that answers like a running hangar (one with state: its replicas, and what it was told), so `down`, `off`
# and `up` can run in sequence. With 0 replicas there is no pod to exec into, and hangar isn't ready.
KUBECTL = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
line = " ".join(a)
log = open(os.environ["KUBECTL_LOG"], "a")
calls = open(os.environ["CALL_LOG"], "a")
path = os.environ["KUBECTL_STATE"]
st = json.load(open(path))
def save():
    json.dump(st, open(path, "w"))
if "exec" in a:
    sys.stdin.read()
    rest = a[a.index("-") + 1:]
    log.write(json.dumps(["exec", *rest]) + "\n")
    calls.write("exec " + " ".join(rest) + "\n")
    if st["replicas"] == 0:
        sys.stderr.write("error: no pod\n")
        sys.exit(1)
    what = " ".join(rest[:2])
    if rest[:1] in (["0"], ["1"]):
        print("no sessions running")
    elif "/api/admin/power/prepull" in what:
        print(json.dumps({"images": ["r/env@sha256:1", "r/pack@sha256:2"], "missing": [],
                          "manifest": {"kind": "DaemonSet", "metadata": {"name": "hangar-prepull"}}}))
    elif what == "POST /api/admin/power/state":
        st.setdefault("told", []).append(json.loads(rest[2])["state"])
        print(json.dumps({"state": {}, "released_jobs": ["job-1"], "warm_pools": {"py": 2}}))
    elif what == "GET /api/admin/power/hold":
        print(json.dumps({"record": st.get("hold")}))
    elif what == "POST /api/admin/power/hold":
        b = json.loads(rest[2])
        st["hold"] = {"until": b["until"], "set_at": b["set_at"], "by": "admin"}
        print("{}")
    elif what == "POST /api/admin/power/release":
        st["hold"] = {"until": None, "set_at": json.loads(rest[2])["set_at"], "by": "admin"}
        print("{}")
    elif what == "GET /api/admin/power":  # --- #186: whether the overnight lane is on
        print(json.dumps({"power": {"state": "up", "overnight": st.get("overnight") or {"on": False}}}))
    elif what == "POST /api/admin/power/overnight":
        b = json.loads(rest[2])
        if b["on"] and st.get("yield_low") and not b["force"]:
            sys.stderr.write("HTTP Error 409: Conflict\n")
            sys.exit(1)
        st["overnight"] = {"on": b["on"], "forced": bool(b["on"] and b["force"] and st.get("yield_low"))}
        print(json.dumps(st["overnight"]))
    elif what == "GET /api/admin/deploys":
        print(json.dumps(st.get("deploys", [])))
    elif what == "POST /api/admin/deploys/pending":
        st["pending"] = json.loads(rest[2])
        print("{}")
    save()
    sys.exit(0)
log.write(line + "\n")
calls.write("kubectl " + line + "\n")
if "readyReplicas" in line:
    print("1" if st["replicas"] else "", end="")
elif ".spec.replicas" in line:
    print(st["replicas"], end="")
elif "numberReady" in line:
    print("1", end="")
elif 'containers[?(@.name=="hangar")].image' in line:
    print(st.get("image", ""), end="")
elif a[:3] == ["-n", "hangar", "scale"]:
    st["replicas"] = int(next(x for x in a if x.startswith("--replicas=")).split("=")[1])
    save()
elif "get pods" in line:
    print("" if st["replicas"] == 0 else "pod/hangar-1")
elif "get nodes" in line:
    print("gke-n1   Ready   <none>   1m   v1.35")
elif a[:2] == ["apply", "-f"]:
    open(os.environ["KUBECTL_APPLIED"], "w").write(sys.stdin.read())
"""

# gh and curl stand-ins (--- infra-state): GitHub's API for the catch-up deploy, with what each call returns set by
# the test; every call logged. Without FAKE_GH_LOGGED_IN, gh isn't logged in (the scheduled job has no gh at all).
GH = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(["gh", *a]) + "\n")
cfg = json.loads(os.environ.get("FAKE_GH", "{}"))
if a[:2] == ["auth", "status"]:
    sys.exit(0 if cfg.get("logged_in") else 1)
if a[:2] == ["repo", "view"]:
    print(cfg["repo"])
elif a[:1] == ["api"] and a[1].endswith("/commits/main"):
    if not cfg.get("head"):
        sys.exit(1)  # GitHub not answering
    print(cfg["head"] if a[2:] == ["--jq", ".sha"] else json.dumps({"sha": cfg["head"]}))
elif a[:1] == ["api"] and "/compare/" in a[1]:
    print(json.dumps({"status": cfg["compare"], "ahead_by": 3}))
elif a[:2] == ["run", "list"]:
    print(json.dumps(cfg.get("runs", [])))
elif a[:2] == ["workflow", "run"]:
    pass
else:
    sys.exit(1)
"""

CURL = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(["curl", *a]) + "\n")
cfg = json.loads(os.environ.get("FAKE_GH", "{}"))
url = a[-1]
if url.endswith("/commits/main"):
    print(json.dumps({"sha": cfg["head"]}))
elif "/compare/" in url:
    print(json.dumps({"status": cfg["compare"], "ahead_by": 3}))
else:
    sys.exit(22)
"""


@pytest.fixture
def cluster(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(GCLOUD_STATEFUL)
    (bin_dir / "kubectl").write_text(KUBECTL)
    (bin_dir / "gh").write_text(GH)
    (bin_dir / "curl").write_text(CURL)
    (bin_dir / "gke-gcloud-auth-plugin").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"labels": {}, "pools": {
        "system": {"on": True, "min": 1, "max": 2, "size": 1},
        "sandboxes-gvisor": {"on": True, "min": 1, "max": 10, "size": 3},
        "sandboxes-microvm": {"on": False, "min": None, "max": None, "size": 2}}}))
    hangar = tmp_path / "hangar.json"
    hangar.write_text(json.dumps({"replicas": 1}))
    env = {k: v for k, v in os.environ.items()
           if k not in ("HANGAR_POWER_INSTALL", "POWER_SCHEDULED", "POWER_DEPLOY_REPO", "GH_TOKEN", "GITHUB_TOKEN")}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", PROJECT="p", ZONE="z", CLUSTER="c",
               GCLOUD_LOG=str(tmp_path / "gcloud.log"), GCLOUD_STATE=str(state), CALL_LOG=str(tmp_path / "calls.log"),
               KUBECTL_LOG=str(tmp_path / "kubectl.log"), KUBECTL_APPLIED=str(tmp_path / "applied.json"),
               KUBECTL_STATE=str(hangar), GH_LOG=str(tmp_path / "gh.log"),
               POWER_UP_CRON="50 9 * * *", POWER_TIME_ZONE="America/New_York", POWER_WARM_MINUTES="10")

    def run(*args: str, ok: bool = True, **extra: str) -> str:
        r = subprocess.run(["bash", str(ROOT / "deploy/gke/power.sh"), *args], env={**env, **extra},
                           capture_output=True, text=True, timeout=60)
        assert (r.returncode == 0) == ok, r.stdout + r.stderr
        return r.stdout + r.stderr

    def kubectl() -> list:
        log = tmp_path / "kubectl.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [json.loads(x) if x.startswith("[") else x for x in lines]

    def lines(name: str) -> list[str]:
        p = tmp_path / name
        return p.read_text().splitlines() if p.exists() else []

    def clear() -> None:
        for name in ("gcloud.log", "kubectl.log", "calls.log", "gh.log"):
            (tmp_path / name).unlink(missing_ok=True)

    return {"run": run, "state": lambda: json.loads(state.read_text()), "kubectl": kubectl, "tmp": tmp_path,
            "hangar": lambda: json.loads(hangar.read_text()),
            "set_hangar": lambda d: hangar.write_text(json.dumps(d)),
            "set_state": lambda d: state.write_text(json.dumps(d)),
            "calls": lambda: lines("calls.log"), "gh": lambda: [json.loads(x) for x in lines("gh.log")],
            "clear": clear}


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


# ====================================================================== --- infra-state: hold, off, catch-up, CI
def _summary(cluster, **extra) -> dict:
    return json.loads(cluster["run"]("state", **extra).strip().splitlines()[-1])


def _at(calls: list[str], *parts: str, last: bool = False) -> int:
    found = [i for i, c in enumerate(calls) if all(p in c for p in parts)]
    assert found, (parts, calls)
    return found[-1] if last else found[0]


def test_a_hold_skips_the_scheduled_power_down_and_power_up(cluster):
    out = cluster["run"]("hold", "--days", "3")
    labels = cluster["state"]()["labels"]
    until = int(labels["hangar-power-hold"])
    assert 3 * 86400 - 120 < until - time.time() <= 3 * 86400 and "held until" in out
    assert cluster["hangar"]()["hold"]["until"] == until  # hangar was told: `hangar power`, status and doctor show it
    before = cluster["state"]()["pools"]
    cluster["clear"]()
    out = cluster["run"]("down", POWER_SCHEDULED="1")
    assert "skipping the scheduled power-down" in out, out
    calls = cluster["calls"]()
    assert "clusters describe" in calls[0]  # the hold is the first thing it looks at
    assert cluster["state"]()["pools"] == before
    assert not any("resize" in c or "autoscaling" in c or "power/state" in c or c == "exec 1" for c in calls), calls
    assert _summary(cluster)["summary"] == "held"
    # By hand, `down` still runs: a hold pauses only the schedule. The scheduled power-up then skips too.
    cluster["run"]("down")
    assert cluster["state"]()["pools"]["sandboxes-gvisor"]["size"] == 0
    out = cluster["run"]("up", POWER_SCHEDULED="1")
    assert "skipping the scheduled power-up" in out and cluster["state"]()["pools"]["sandboxes-gvisor"]["size"] == 0
    assert cluster["state"]()["labels"]["hangar-power-hold"] == str(until)
    # `power.sh release` ends it: the next scheduled run goes ahead
    cluster["run"]("release")
    assert "hangar-power-hold" not in cluster["state"]()["labels"] and cluster["hangar"]()["hold"]["until"] is None
    cluster["run"]("up", POWER_SCHEDULED="1")
    assert cluster["state"]()["pools"]["sandboxes-gvisor"]["size"] == 1


def test_hold_until_a_date_and_what_it_refuses(cluster):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    day = (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=10)).date()
    cluster["run"]("hold", "--until", day.isoformat())  # that day's 00:00, in POWER_TIME_ZONE
    want = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("America/New_York")).timestamp()
    assert cluster["state"]()["labels"]["hangar-power-hold"] == str(int(want))
    for bad, says in ((["--until", "2001-01-01"], "has passed"), (["--days", "91"], "between 0 and 90"),
                      (["--until", "someday"], "isn't a date"), ([], "--days N or --until DATE"),
                      (["--days", "1", "--until", day.isoformat()], "not both")):
        assert says in cluster["run"]("hold", *bad, ok=False), bad
    assert cluster["state"]()["labels"]["hangar-power-hold"] == str(int(want))  # unchanged by the refusals


def test_hangar_power_hold_reaches_the_scheduled_job_and_the_newer_side_wins(cluster):
    """`hangar power hold` is recorded in hangar (it can't write the cluster's labels); the scheduled job copies it
    to the cluster before deciding. `hangar power release`, newer still, lets the next run go ahead."""
    now = int(time.time())
    cluster["set_hangar"]({"replicas": 1, "hold": {"until": now + 86400, "set_at": now - 5, "by": "sean"}})
    out = cluster["run"]("down", POWER_SCHEDULED="1")
    assert "skipping the scheduled power-down" in out
    labels = cluster["state"]()["labels"]
    assert labels["hangar-power-hold"] == str(now + 86400) and labels["hangar-power-hold-set"] == str(now - 5)
    assert cluster["state"]()["pools"]["sandboxes-gvisor"]["size"] == 3
    cluster["set_hangar"]({"replicas": 1, "hold": {"until": None, "set_at": now, "by": "sean"}})
    out = cluster["run"]("down", POWER_SCHEDULED="1")
    assert "hangar ended the hold" in out and "hangar-power-hold" not in cluster["state"]()["labels"]
    assert cluster["state"]()["pools"]["sandboxes-gvisor"]["size"] == 0
    # The cluster's side newer (power.sh hold while hangar had an older record): hangar is told
    st = cluster["state"]()
    st["labels"].update({"hangar-power-hold": str(now + 7200), "hangar-power-hold-set": str(now + 1)})
    cluster["set_state"](st)
    cluster["run"]("up", POWER_SCHEDULED="1")
    assert cluster["hangar"]()["hold"] == {"until": now + 7200, "set_at": now + 1, "by": "admin"}


def test_an_ended_hold_resumes_the_schedule_and_powers_nothing_up_early(cluster):
    now = int(time.time())
    st = cluster["state"]()
    st["labels"].update({"hangar-power-hold": str(now - 60), "hangar-power-hold-set": str(now - 86400)})
    cluster["set_state"](st)
    assert _summary(cluster)["summary"] == "up" and _summary(cluster)["held"] is False
    out = cluster["run"]("down", POWER_SCHEDULED="1")
    assert "the hold ended" in out and "hangar-power-hold" not in cluster["state"]()["labels"]
    assert cluster["state"]()["pools"]["sandboxes-gvisor"]["size"] == 0  # the run it would have done anyway


def test_off_then_up_brings_everything_back_in_order(cluster):
    out = cluster["run"]("off", "--days", "5")
    st = cluster["state"]()
    assert {p: v["size"] for p, v in st["pools"].items()} == {"system": 0, "sandboxes-gvisor": 0,
                                                             "sandboxes-microvm": 0}
    assert cluster["hangar"]()["replicas"] == 0 and "Nothing was deleted" in out
    labels = st["labels"]
    assert labels["hangar-power-system"] == "1-2" and labels["hangar-power-sandboxes-gvisor"] == "1-10"
    assert labels["hangar-power-sandboxes-microvm"] == "fixed-2" and "hangar-power-off" in labels
    assert 5 * 86400 - 120 < int(labels["hangar-power-hold"]) - time.time() <= 5 * 86400
    calls = cluster["calls"]()
    order = [_at(calls, "--update-labels=hangar-power-hold="),  # the hold first: a scheduled up meanwhile skips
             _at(calls, "exec POST /api/admin/power/state"), _at(calls, "exec 1"),  # tell hangar, drain, suspend
             _at(calls, "resize c", "--node-pool sandboxes-gvisor --num-nodes 0"),
             _at(calls, "scale deploy/hangar --replicas=0"), _at(calls, "get pods -l app=hangar"),  # stopped, gone
             _at(calls, "resize c", "--node-pool system --num-nodes 0")]  # only then its node
    assert order == sorted(order), calls
    assert not any(w in c for c in calls for w in (" delete", "pvc", "disks")), "off deletes nothing"
    assert cluster["hangar"]()["hold"]["until"] == int(labels["hangar-power-hold"])
    assert _summary(cluster)["summary"] == "off"
    # Off and held: the scheduled runs skip
    for action in ("up", "down"):
        assert "skipping the scheduled" in cluster["run"](action, POWER_SCHEDULED="1")
    assert cluster["hangar"]()["replicas"] == 0 and cluster["state"]()["pools"]["system"]["size"] == 0

    cluster["clear"]()
    cluster["run"]("up")
    st = cluster["state"]()
    assert st["pools"] == {"system": {"on": True, "min": 1, "max": 2, "size": 1},
                           "sandboxes-gvisor": {"on": True, "min": 1, "max": 10, "size": 1},
                           "sandboxes-microvm": {"on": False, "min": None, "max": None, "size": 2}}
    assert list(st["labels"]) == ["hangar-power-hold-set"]  # off, the hold and the saved ranges are gone
    h = cluster["hangar"]()
    assert h["replicas"] == 1 and h["told"][-2:] == ["starting", "up"] and h["hold"]["until"] is None
    calls = cluster["calls"]()
    order = [_at(calls, "resize c", "--node-pool system --num-nodes 1"),
             _at(calls, "get nodes -l cloud.google.com/gke-nodepool=system"),
             _at(calls, "scale deploy/hangar --replicas=1"), _at(calls, "rollout status deploy/hangar"),
             _at(calls, "exec POST /api/admin/power/state"),  # starting, once hangar is ready
             _at(calls, "resize c", "--node-pool sandboxes-gvisor --num-nodes 1"),
             _at(calls, "get nodes -l cloud.google.com/gke-nodepool=sandboxes-gvisor"),
             _at(calls, "kubectl apply -f"),  # the prepull
             _at(calls, "exec POST /api/admin/power/state", last=True)]  # up: queued jobs start
    assert order == sorted(order), calls
    assert _summary(cluster)["summary"] == "up"


def test_off_needs_kubectl_and_changes_nothing_without_it(cluster):
    (cluster["tmp"] / "bin" / "gke-gcloud-auth-plugin").unlink()
    out = cluster["run"]("off", ok=False)
    assert "Nothing was changed" in out and cluster["state"]()["labels"] == {}
    assert cluster["state"]()["pools"]["system"]["size"] == 1


def test_off_keeps_the_system_node_while_hangar_is_still_stopping(cluster):
    """Hangar's database is on the hangar-data volume, attached to the system node: that node goes only once the
    pod is gone, never while it may still be writing."""
    kubectl = cluster["tmp"] / "bin" / "kubectl"
    kubectl.write_text(kubectl.read_text().replace('print("" if st["replicas"] == 0 else "pod/hangar-1")',
                                                   'print("pod/hangar-1")'))
    out = cluster["run"]("off", ok=False)
    assert "hasn't stopped" in out and cluster["state"]()["pools"]["system"]["size"] == 1


def test_the_scheduled_power_down_fails_closed_without_hangar(cluster):
    """Without hangar there's no drain (running turns would be cut off) and no reading `hangar power hold`: the
    scheduled power-down leaves the pools up and fails, visibly. By hand it still goes ahead."""
    cluster["set_hangar"]({"replicas": 0})  # crash-looping, say; the cluster isn't off
    out = cluster["run"]("down", POWER_SCHEDULED="1", ok=False)
    assert "not powering down" in out and cluster["state"]()["pools"]["sandboxes-gvisor"]["size"] == 3
    assert "hangar-power-lock" not in cluster["state"]()["labels"]  # released on the way out
    cluster["run"]("down")
    assert cluster["state"]()["pools"]["sandboxes-gvisor"]["size"] == 0


def test_a_hangar_hold_that_cant_be_copied_to_the_cluster_still_stops_the_power_down(cluster):
    now = int(time.time())
    cluster["set_hangar"]({"replicas": 1, "hold": {"until": now + 86400, "set_at": now, "by": "sean"}})
    out = cluster["run"]("down", POWER_SCHEDULED="1", ok=False, GCLOUD_FAIL="--update-labels=hangar-power-hold=")
    assert "not powering down" in out and cluster["state"]()["pools"]["sandboxes-gvisor"]["size"] == 3


def test_a_half_finished_off_counts_as_off_for_the_schedule(cluster):
    """`off` stopped everything but couldn't mark the cluster off: the system pool's saved range says it is."""
    st = cluster["state"]()
    st["labels"] = {"hangar-power-system": "1-2", "hangar-power-sandboxes-gvisor": "1-10"}
    st["pools"]["system"]["size"] = 0
    cluster["set_state"](st)
    cluster["set_hangar"]({"replicas": 0})
    assert "the cluster is off" in cluster["run"]("down", POWER_SCHEDULED="1")


def test_one_power_run_at_a_time(cluster):
    st = cluster["state"]()
    st["labels"]["hangar-power-lock"] = f"{int(time.time()) - 60}-123"  # a run that started a minute ago
    cluster["set_state"](st)
    assert "another power run" in cluster["run"]("up", ok=False)
    for action in ("down", "up"):  # the scheduled ones do nothing and fail, so the job shows it (and retries once)
        assert "another power run" in cluster["run"](action, POWER_SCHEDULED="1", ok=False)
    assert cluster["state"]()["pools"]["sandboxes-gvisor"]["size"] == 3
    assert cluster["state"]()["labels"]["hangar-power-lock"] == st["labels"]["hangar-power-lock"]  # not ours
    st["labels"]["hangar-power-lock"] = f"{int(time.time()) - 3 * 3600}-123"  # one that died: taken over
    cluster["set_state"](st)
    cluster["run"]("down")
    assert cluster["state"]()["pools"]["sandboxes-gvisor"]["size"] == 0
    assert "hangar-power-lock" not in cluster["state"]()["labels"]


def test_a_failed_resize_keeps_the_saved_size_for_the_next_up(cluster):
    cluster["run"]("down")
    cluster["run"]("up", ok=False, GCLOUD_FAIL="--node-pool sandboxes-microvm --num-nodes 2")
    st = cluster["state"]()
    assert st["labels"]["hangar-power-sandboxes-microvm"] == "fixed-2" and st["pools"]["sandboxes-microvm"]["size"] == 0
    cluster["run"]("up")
    assert cluster["state"]()["pools"]["sandboxes-microvm"]["size"] == 2


def test_a_size_that_cant_be_read_stops_down_before_the_pool_goes(cluster):
    out = cluster["run"]("down", ok=False, GCLOUD_FAIL="instance-groups managed describe")
    st = cluster["state"]()
    assert "couldn't read sandboxes-microvm's size" in out and st["pools"]["sandboxes-microvm"]["size"] == 2
    assert "hangar-power-sandboxes-microvm" not in st["labels"]


def test_off_stops_when_hangars_pods_cant_be_listed(cluster):
    kubectl = cluster["tmp"] / "bin" / "kubectl"
    kubectl.write_text(kubectl.read_text().replace('elif "get pods" in line:',
                                                   'elif "get pods" in line:\n    sys.exit(1)'))
    out = cluster["run"]("off", ok=False)
    st = cluster["state"]()
    assert "couldn't be listed" in out and st["pools"]["system"]["size"] == 1
    assert "hangar-power-off" not in st["labels"] and "hangar-power-hold" in st["labels"]  # held; not marked off


@pytest.mark.parametrize("labels, summary", [
    ({}, "up"),
    ({"hangar-power-sandboxes-gvisor": "1-10"}, "down"),
    ({"hangar-power-sandboxes-gvisor": "1-10", "hangar-power-hold": "9999999999"}, "held"),
    ({"hangar-power-hold": "9999999999"}, "held"),  # held while up
    ({"hangar-power-off": "1", "hangar-power-system": "1-2", "hangar-power-hold": "9999999999"}, "off"),
    ({"hangar-power-system": "1-2"}, "off"),  # an older power.sh took everything down
    ({"hangar-power-sandboxes-gvisor": "1-10", "hangar-power-starting": "NOW"}, "starting"),
    ({"hangar-power-starting": "1"}, "up"),  # an `up` that died over an hour ago
])
def test_state_is_read_from_the_labels_alone(cluster, labels, summary):
    st = cluster["state"]()
    st["labels"] = {k: str(int(time.time())) if v == "NOW" else v for k, v in labels.items()}
    cluster["set_state"](st)
    assert _summary(cluster)["summary"] == summary
    assert cluster["calls"]() == ["gcloud container clusters describe c --project p --zone z "
                                  "--format=json(resourceLabels)"]  # read-only: one get


HEAD, OLD = "a" * 40, "b" * 40


def _workflow_runs(cluster) -> list:
    return [c for c in cluster["gh"]() if c[:3] == ["gh", "workflow", "run"]]


def test_up_starts_deploy_yml_only_when_main_is_ahead(cluster):
    cluster["set_hangar"]({"replicas": 1, "deploys": [{"sha": OLD, "result": "passed"}]})
    gh = {"logged_in": True, "repo": "o/r", "head": HEAD, "compare": "ahead"}
    out = cluster["run"]("up", FAKE_GH=json.dumps(gh))
    assert _workflow_runs(cluster) == [["gh", "workflow", "run", "deploy.yml", "--repo", "o/r", "--ref", "main"]]
    assert "started deploy.yml for main" in out and "Approve it" in out
    assert any(c[:2] == ["gh", "api"] and c[2] == f"repos/o/r/compare/{OLD}...{HEAD}" for c in cluster["gh"]())
    assert cluster["hangar"]()["pending"] == {"repo": "o/r", "head": HEAD, "deployed": OLD, "ahead": 3,
                                              "started": True}
    for compare in ("identical", "behind", "diverged"):  # hangar runs main's head (or a tag past it): nothing
        cluster["clear"]()
        cluster["run"]("up", FAKE_GH=json.dumps({**gh, "compare": compare}))
        assert _workflow_runs(cluster) == [], compare
    cluster["clear"]()  # a deploy.yml run already queued, or waiting for approval: not a second
    cluster["run"]("up", FAKE_GH=json.dumps({**gh, "runs": [{"status": "waiting"}]}))
    assert _workflow_runs(cluster) == []
    cluster["clear"]()  # no deploy recorded yet: the running image's tag says which commit it is
    cluster["set_hangar"]({"replicas": 1, "image": f"r/control-plane:{OLD}@sha256:{'c' * 64}"})
    cluster["run"]("up", FAKE_GH=json.dumps(gh))
    assert len(_workflow_runs(cluster)) == 1
    cluster["clear"]()
    cluster["run"]("up", FAKE_GH=json.dumps(gh), POWER_CATCH_UP="0")
    assert cluster["gh"]() == []


def test_the_scheduled_power_up_never_starts_a_deploy(cluster):
    """It holds no GitHub credential: it compares anonymously (a public repository), tells hangar, and says how."""
    cluster["set_hangar"]({"replicas": 1, "deploys": [{"sha": OLD, "result": "passed"}]})
    out = cluster["run"]("up", POWER_SCHEDULED="1", POWER_DEPLOY_REPO="o/r",
                         FAKE_GH=json.dumps({"head": HEAD, "compare": "ahead"}))
    assert _workflow_runs(cluster) == []
    assert any(c[0] == "curl" and c[-1] == f"https://api.github.com/repos/o/r/compare/{OLD}...{HEAD}"
               for c in cluster["gh"]())
    assert "gh workflow run deploy.yml --repo o/r --ref main" in out
    assert cluster["hangar"]()["pending"]["started"] is False


# ---------------------------------------------------------------------- deploy.yml: a merge deploys only while up
def _decide(cluster, tmp_path, event: str, ref: str, labels: dict, moved: bool | None = False,
            signed_in: bool = True):
    doc = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    step = next(s for s in doc["jobs"]["state"]["steps"] if s.get("id") == "decide")
    st = cluster["state"]()
    st["labels"] = labels
    cluster["set_state"](st)
    out, summary = tmp_path / "out", tmp_path / "summary"
    out.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "POWER_"))}
    env.update(PATH=f"{cluster['tmp'] / 'bin'}:{env['PATH']}", GITHUB_OUTPUT=str(out),
               GITHUB_STEP_SUMMARY=str(summary), GITHUB_SHA=HEAD, GITHUB_REF=ref, GITHUB_REPOSITORY="o/r",
               GITHUB_RUN_NUMBER="41", EVENT=event, SIGNED_IN="true" if signed_in else "false", GH_TOKEN="t",
               HANGAR_CLUSTER="projects/p/locations/z/clusters/c",
               FAKE_GH=json.dumps({"head": None if moved is None else "c" * 40 if moved else HEAD}),  # main's head
               GCLOUD_LOG=str(tmp_path / "g.log"), GCLOUD_STATE=str(cluster["tmp"] / "state.json"),
               CALL_LOG=str(tmp_path / "c.log"), GH_LOG=str(tmp_path / "gh.log"))
    r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    deploy = "refused" if r.returncode else out.read_text().strip().removeprefix("deploy=")
    return deploy, r.stdout + r.stderr


MAIN = "refs/heads/main"


@pytest.mark.parametrize("event, ref, labels, moved, deploy, says", [
    ("push", MAIN, {}, False, "true", None),
    ("push", MAIN, {"hangar-power-sandboxes-gvisor": "1-10"}, False, "false",
     f"infra is down; not deploying {HEAD[:12]}; it will deploy on the next power-up"),
    ("push", MAIN, {"hangar-power-hold": "9999999999"}, False, "false", "infra is held; not deploying"),
    ("push", MAIN, {"hangar-power-off": "1", "hangar-power-system": "1-2"}, False, "false",
     "infra is off; not deploying"),
    ("push", MAIN, {}, True, "false", "main has moved on to cccccccccccc"),  # a burst of merges: the newest deploys
    ("push", MAIN, {}, None, "false", "Couldn't read main's head"),  # it might be an old commit: not deployed
    ("workflow_dispatch", MAIN, {"hangar-power-sandboxes-gvisor": "1-10"}, False, "true", None),  # the deploy job
    ("workflow_dispatch", MAIN, {"hangar-power-off": "1"}, False, "refused", "hangar is off"),
    ("push", "refs/tags/v1.2.0", {"hangar-power-hold": "9999999999"}, False, "true", None),  # a release tag
])
def test_a_merge_deploys_only_while_the_infra_is_up(cluster, tmp_path, event, ref, labels, moved, deploy, says):
    got, out = _decide(cluster, tmp_path, event, ref, labels, moved)
    assert got == deploy, out
    if says:
        assert says in out, out
    if deploy == "false":
        assert ("::notice title=deploy:" in out or "::warning title=deploy:" in out) and "::error" not in out


def test_a_merge_without_the_state_check_set_up_skips_with_a_warning(cluster, tmp_path):
    got, out = _decide(cluster, tmp_path, "push", MAIN, {}, signed_in=False)
    assert got == "false" and "::warning title=deploy: skipped::" in out and "GCP_STATE_SERVICE_ACCOUNT" in out


def test_no_merge_deploys_without_the_owners_approval():
    """The state check runs before any approval, so it holds nothing that deploys: its own read-only account, no
    hangar token, no kubectl. Every job that touches the cluster or hangar's admin API is the production
    environment's job, which needs every earlier job and the owner's approval; a merge reaches it only through the
    state job's `deploy` output."""
    doc = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    jobs = doc["jobs"]
    state = json.dumps(jobs["state"])
    assert "GCP_STATE_SERVICE_ACCOUNT" in state and "environment" not in jobs["state"]
    assert jobs["state"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert jobs["deploy"]["permissions"] == {"contents": "read", "id-token": "write"}
    for forbidden in ("HANGAR_DEPLOY_TOKEN", "GCP_DEPLOY_SERVICE_ACCOUNT", "secrets.GCP_SERVICE_ACCOUNT", "kubectl",
                      "get-gke-credentials", "HANGAR_URL"):
        assert forbidden not in state, forbidden
    for name, job in jobs.items():
        text = json.dumps(job)
        if "kubectl" in text or "HANGAR_DEPLOY_TOKEN" in text or "get-gke-credentials" in text:
            assert name == "deploy", name
    deploy = jobs["deploy"]
    assert deploy["environment"] == "production" and "state" in deploy["needs"]
    assert deploy["if"] == "needs.state.outputs.deploy == 'true'"
    for name in ("images", "control-plane"):
        assert jobs[name]["if"] == "needs.state.outputs.deploy == 'true'", name
    steps = {s.get("name") or s.get("uses"): s for s in deploy["steps"]}
    for name in ("Drain (creates and destroys in flight finish before hangar restarts)", "Roll out", "Acceptance"):
        assert "steps.pre.outputs.go == 'true'" in steps[name]["if"], name
    assert doc["concurrency"] == {"group": "deploy-production", "cancel-in-progress": False}
    images = yaml.safe_load((ROOT / ".github/workflows/images.yml").read_text())
    assert "github.workflow" in images["concurrency"]["group"]  # a deploy's build isn't cancelled by a push's
    assert images[True]["push"] == {"branches": ["main"]}  # images build on every merge, whatever the power state


def test_power_sh_shellcheck_is_clean():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck isn't installed (CI's lint job runs it)")
    r = subprocess.run(["shellcheck", str(ROOT / "deploy/gke/power.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


# ====================================================================== --- #186: the overnight lane
def _with_lane(cluster, on: bool) -> None:
    st = cluster["state"]()
    st["pools"]["sandboxes-overnight"] = {"on": False, "min": None, "max": None, "size": 0}
    cluster["set_state"](st)
    cluster["set_hangar"]({**cluster["hangar"](), "overnight": {"on": on}})


def test_down_with_the_lane_on_leaves_one_overnight_node_and_says_night(cluster):
    _with_lane(cluster, on=True)
    cluster["run"]("down")
    st = cluster["state"]()
    assert st["pools"]["sandboxes-overnight"]["size"] == 1  # exactly one node, never saved as a range to restore
    assert st["pools"]["sandboxes-gvisor"]["size"] == 0 and st["pools"]["sandboxes-microvm"]["size"] == 0
    assert "hangar-power-sandboxes-overnight" not in st["labels"] and st["labels"]["hangar-power-overnight"] == "on"
    assert [t["state"] for t in _told(cluster["kubectl"]())] == ["night"]
    assert _summary(cluster)["state"] == "down"  # the lane's label isn't a saved pool
    # the morning: the other pools first, hangar told, then the lane's node goes
    cluster["clear"]()
    cluster["run"]("up")
    st = cluster["state"]()
    assert st["pools"]["sandboxes-overnight"]["size"] == 0 and st["pools"]["sandboxes-gvisor"]["size"] == 1
    calls = cluster["calls"]()
    i_up = max(i for i, c in enumerate(calls) if "/api/admin/power/state" in c)
    i_lane = _at(calls, "resize", "--node-pool sandboxes-overnight", "--num-nodes 0")
    assert i_up < i_lane


def test_down_with_the_lane_off_or_no_pool_is_a_plain_down(cluster):
    _with_lane(cluster, on=False)
    cluster["run"]("down")
    st = cluster["state"]()
    assert st["pools"]["sandboxes-overnight"]["size"] == 0 and "hangar-power-overnight" not in st["labels"]
    assert [t["state"] for t in _told(cluster["kubectl"]())] == ["down"]


def test_power_sh_overnight_goes_through_hangars_yield_gate(cluster):
    out = cluster["run"]("overnight", "on", ok=False)
    assert "no sandboxes-overnight node pool" in out
    _with_lane(cluster, on=False)
    cluster["set_hangar"]({**cluster["hangar"](), "yield_low": True})
    out = cluster["run"]("overnight", "on", ok=False)
    assert "hangar refused" in out and "hangar-power-overnight" not in cluster["state"]()["labels"]
    out = cluster["run"]("overnight", "on", "--force")
    assert "overnight lane on (forced)" in out and cluster["state"]()["labels"]["hangar-power-overnight"] == "on"
    assert cluster["hangar"]()["overnight"]["on"] is True
    st = cluster["state"]()
    st["pools"]["sandboxes-overnight"]["size"] = 1  # at night
    cluster["set_state"](st)
    cluster["run"]("overnight", "off")
    st = cluster["state"]()
    assert "hangar-power-overnight" not in st["labels"] and st["pools"]["sandboxes-overnight"]["size"] == 0
