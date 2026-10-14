"""`hangar up --gke` and deploy/gke/up.sh, against fake terraform, gcloud and kubectl on PATH.

The fakes log every call, so the tests can prove what did and didn't run: --dry-run and a refused plan never reach
`terraform apply` or up.sh."""

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

import hangar.cli as cli
from hangar import setup_cli, setup_gke

runner = CliRunner()
REPO = Path(__file__).resolve().parents[1]

FAKE = textwrap.dedent('''\
    #!{python}
    import json, os, shutil, sys
    from pathlib import Path
    tool, args = Path(sys.argv[0]).name, sys.argv[1:]
    cfg = json.loads(os.environ["FAKE_CFG"])
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps([tool, *args]) + "\\n")
    if tool == "terraform":
        args = [a for a in args if not a.startswith("-chdir=")]
        if args[0] == "plan":
            Path(next(a for a in args if a.startswith("-out="))[5:]).write_text("plan")
        elif args[0] == "show":
            print(json.dumps({{"resource_changes": [{{"address": a, "change": {{"actions": x}}}}
                                                   for a, x in cfg["changes"]]}}))
        elif args[0] == "output":
            v = cfg["outputs"].get(args[-1])
            if v is None:
                sys.exit(1)
            print(v, end="")
    elif tool == "kubectl":
        line = " ".join(args)
        if "get secret" in line or "get deployment" in line and not cfg.get("deployed"):
            sys.exit(1)
        if "get configmap" in line:
            v = cfg.get("live", {{}}).get(args[args.index("configmap") + 1])
            if v is None:
                sys.exit(1)
            print(v, end="")
        if args[:1] == ["apply"] and "-k" in args:
            shutil.copy(Path(args[args.index("-k") + 1]) / "config.yaml", os.environ["FAKE_CAPTURE"])
    ''')


@pytest.fixture
def gke(tmp_path, monkeypatch):
    """A hangar checkout (deploy/ only, in git, as up.sh expects) and fake cloud tools."""
    root = tmp_path / "hangar"
    shutil.copytree(REPO / "deploy", root / "deploy")
    (root / "deploy/terraform/gke/terraform.tfvars").write_text('project_id = "p"\n')
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run([*git, "-C", str(root), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(root), "commit", "-qm", "x"], check=True)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for tool in ("terraform", "gcloud", "kubectl", "hangar"):  # --- images: hangar = up.sh's prebuild
        (bin_ / tool).write_text(FAKE.format(python=sys.executable))
        (bin_ / tool).chmod(0o755)
    log, capture = tmp_path / "calls.log", tmp_path / "config.yaml"
    log.touch()
    cfg = {"changes": [["google_container_cluster.hangar", ["create"]]], "outputs": {
        "registry": "us-central1-docker.pkg.dev/p/hangar", "service_account": "hangar@p.iam.gserviceaccount.com",
        "static_ip": "34.1.2.3", "get_credentials": "true", "session_bucket": "gs://p-hangar-sessions"}}
    monkeypatch.setenv("PATH", f"{bin_}:{Path(sys.executable).parent}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HANGAR_DATA_DIR", str(tmp_path / "home" / ".hangar"))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_CAPTURE", str(capture))
    monkeypatch.setenv("HANGAR_CLI", str(bin_ / "hangar"))
    monkeypatch.setattr(setup_gke, "checkout", lambda: root)
    monkeypatch.setattr(setup_cli, "_token_ok", lambda url, token: False)  # the certificate isn't Active yet

    class G:
        def set(self, **kw):
            cfg.update(kw)
            monkeypatch.setenv("FAKE_CFG", json.dumps(cfg))

        def outputs(self, **kw):
            return {**cfg["outputs"], **kw}

        def calls(self):
            return [json.loads(x) for x in log.read_text().splitlines()]

        def ran(self, tool, verb):
            return any(c[0] == tool and verb in c for c in self.calls())

        def config(self):
            return capture.read_text() if capture.exists() else None

    g = G()
    g.set()
    return g


def test_dry_run_plans_and_changes_nothing(gke):
    gke.set(changes=[["google_container_cluster.hangar", ["create"]], ["google_compute_global_address.hangar",
                                                                       ["delete", "create"]]])
    r = runner.invoke(cli.app, ["up", "--gke", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "1 to create, 0 to change, 1 to destroy or replace" in r.output
    assert "destroys google_compute_global_address.hangar (replaced)" in r.output
    assert "Dry run: nothing was changed" in r.output
    assert gke.ran("terraform", "plan") and not gke.ran("terraform", "apply") and gke.config() is None
    assert not list(Path(setup_cli._data_dir()).glob("*.tfplan"))  # the plan file is removed


def test_no_tty_no_yes_applies_nothing(gke):
    r = runner.invoke(cli.app, ["up", "--gke"])
    assert r.exit_code == 1 and "pass --yes" in r.output
    assert not gke.ran("terraform", "apply") and gke.config() is None


def test_destroying_plan_needs_allow_destroy_even_with_yes(gke):
    gke.set(changes=[["google_container_cluster.hangar", ["delete", "create"]]])
    r = runner.invoke(cli.app, ["up", "--gke", "--yes"])
    assert r.exit_code == 1 and "won't apply it for you" in r.output
    assert not gke.ran("terraform", "apply") and gke.config() is None
    r = runner.invoke(cli.app, ["up", "--gke", "--yes", "--allow-destroy"])
    assert r.exit_code == 0, r.output
    assert gke.ran("terraform", "apply")


def test_fresh_install_applies_deploys_and_wires_the_session_bucket(gke):
    r = runner.invoke(cli.app, ["up", "--gke", "--yes"])
    assert r.exit_code == 0, r.output
    applied = [c for c in gke.calls() if c[0] == "terraform" and "apply" in c]
    assert len(applied) == 1 and applied[0][-1].endswith("gke.tfplan")  # exactly the plan that was shown
    config = gke.config()
    assert 'HANGAR_SESSION_STORE: "gs://p-hangar-sessions"' in config  # the owner patched this in by hand
    assert "https://34.1.2.3.sslip.io" in config
    assert "hangar login https://34.1.2.3.sslip.io --token-file" in r.output
    assert (Path.home() / ".hangar" / "gke-admin-token").exists()


def test_up_sh_prebuilds_images_and_packs_before_rollout(gke, monkeypatch):
    """--- images: what the new control plane needs is built before it serves (and PREBUILD=0 skips it)."""
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    calls = gke.calls()
    build = next(i for i, c in enumerate(calls) if c[:3] == ["hangar", "images", "build"])
    rollout = next(i for i, c in enumerate(calls) if c[:2] == ["kubectl", "apply"] and "-k" in c)
    assert build < rollout and "kubernetes" in calls[build]
    monkeypatch.setenv("PREBUILD", "0")
    n = len(calls)
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    assert not [c for c in gke.calls()[n:] if c[0] == "hangar"]


def test_up_sh_leaves_a_running_hangars_checkpoints_where_they_are(gke):
    """Switching a running hangar to GCS would strand its suspended sessions' local checkpoints."""
    gke.set(changes=[], deployed=True)
    r = runner.invoke(cli.app, ["up", "--gke", "--yes"])
    assert r.exit_code == 0, r.output
    assert "infrastructure is up to date" in r.output and not gke.ran("terraform", "apply")
    assert "# HANGAR_SESSION_STORE:" in gke.config()
    gke.set(live={"hangar": "gs://kept"})  # already set by an earlier up.sh: kept
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    assert 'HANGAR_SESSION_STORE: "gs://kept"' in gke.config()
    gke.set(outputs=gke.outputs(session_bucket=None), deployed=False, live={})
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    assert "# HANGAR_SESSION_STORE:" in gke.config()  # enable_session_bucket=false: no bucket, nothing set


def test_gke_needs_its_tools_and_tfvars(gke, monkeypatch, tmp_path):
    (setup_gke.checkout() / "deploy/terraform/gke/terraform.tfvars").unlink()
    r = runner.invoke(cli.app, ["up", "--gke"])
    assert r.exit_code == 2 and "terraform.tfvars.example" in r.output
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    r = runner.invoke(cli.app, ["up", "--gke"])
    assert r.exit_code == 2 and "missing terraform, gcloud, kubectl" in r.output
    assert runner.invoke(cli.app, ["up", "--dry-run"]).exit_code == 2  # --dry-run is a --gke option
