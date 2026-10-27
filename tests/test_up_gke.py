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
        if "get deployment" in line:
            print(cfg.get("image", ""), end="")
        if "get configmap" in line:
            v = cfg.get("live", {{}}).get(args[args.index("configmap") + 1])
            if isinstance(v, dict):  # {{key: value}}: the key the jsonpath asks for
                v = v.get(next(a for a in args if a.startswith("jsonpath=")).split(".")[-1].rstrip("}}"))
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
        "static_ip": "34.1.2.3", "get_credentials": "true", "session_bucket": "gs://p-hangar-sessions",
        "run_bucket": "gs://p-hangar-runs", "build_staging": "gs://p-hangar-build-src"}}
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
    assert 'HANGAR_OUTBOX_URL: "gs://p-hangar-runs"' in config  # --- #273: run capture on from the first install
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
    gke.set(live={"hangar": {"HANGAR_SESSION_STORE": "gs://kept"}})  # already set by an earlier up.sh: kept
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    assert 'HANGAR_SESSION_STORE: "gs://kept"' in gke.config()
    gke.set(outputs=gke.outputs(session_bucket=None), deployed=False, live={})
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    assert "# HANGAR_SESSION_STORE:" in gke.config()  # enable_session_bucket=false: no bucket, nothing set


def test_up_sh_turns_the_outbox_on_for_a_running_hangar(gke):
    """--- #273: unlike checkpoints, nothing is stranded by turning run capture on, so a running hangar gets it too,
    and a restart when the image is the same (the outbox reads its URL at startup)."""
    image = "us-central1-docker.pkg.dev/p/hangar/control-plane:" + subprocess.run(
        ["git", "-C", str(setup_gke.checkout()), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
        check=True).stdout.strip()
    gke.set(changes=[], deployed=True, image=image)
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    assert 'HANGAR_OUTBOX_URL: "gs://p-hangar-runs"' in gke.config()
    assert ["kubectl", "-n", "hangar", "rollout", "restart", "deploy/hangar"] in gke.calls()
    n = len(gke.calls())  # on already: kept as it is, and no restart
    gke.set(live={"hangar": {"HANGAR_OUTBOX_URL": "gs://p-hangar-runs/team",
                             "HANGAR_BUILD_STAGING": "gs://p-hangar-build-src"}})
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    assert 'HANGAR_OUTBOX_URL: "gs://p-hangar-runs/team"' in gke.config()
    assert not [c for c in gke.calls()[n:] if "restart" in c]
    gke.set(image="elsewhere/control-plane:old", live={})  # a new image: its rollout restarts the pod anyway
    n = len(gke.calls())
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    assert not [c for c in gke.calls()[n:] if "restart" in c]
    gke.set(outputs=gke.outputs(run_bucket=None))  # enable_run_bucket=false: no bucket, the outbox stays off
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    assert "# HANGAR_OUTBOX_URL:" in gke.config()



def test_up_sh_points_cloud_build_at_hangars_own_staging_bucket(gke):
    """KAN-172: hangar has no project-wide storage role, so Cloud Build stages in the bucket terraform made for it. A
    running hangar gets it too, with a restart when the image is the same (settings are read at startup); a value
    already set is kept."""
    image = "us-central1-docker.pkg.dev/p/hangar/control-plane:" + subprocess.run(
        ["git", "-C", str(setup_gke.checkout()), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
        check=True).stdout.strip()
    gke.set(changes=[], deployed=True, image=image,
            live={"hangar": {"HANGAR_OUTBOX_URL": "gs://p-hangar-runs"}})
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    assert 'HANGAR_BUILD_STAGING: "gs://p-hangar-build-src"' in gke.config()
    assert ["kubectl", "-n", "hangar", "rollout", "restart", "deploy/hangar"] in gke.calls()
    n = len(gke.calls())
    gke.set(live={"hangar": {"HANGAR_OUTBOX_URL": "gs://p-hangar-runs", "HANGAR_BUILD_STAGING": "gs://mine/x"}})
    assert runner.invoke(cli.app, ["up", "--gke", "--yes"]).exit_code == 0
    assert 'HANGAR_BUILD_STAGING: "gs://mine/x"' in gke.config()
    assert not [c for c in gke.calls()[n:] if "restart" in c]


def test_terraform_scopes_hangars_roles():
    """KAN-172: no project-wide storage or registry role for hangar's service account; the registry writer is on the
    hangar repository and storage admin on its staging bucket only."""
    import re

    main = (REPO / "deploy/terraform/gke/main.tf").read_text()
    project = re.search(r'resource "google_project_iam_member" "hangar" \{(.*?)\n\}', main, re.S).group(1)
    roles = set(re.findall(r'"(roles/[^"]+)"', project))
    assert roles == {"roles/cloudbuild.builds.editor", "roles/logging.logWriter"}, roles
    writer = re.search(r'resource "google_artifact_registry_repository_iam_member" "hangar_writer" \{(.*?)\n\}',
                       main, re.S).group(1)
    assert "google_artifact_registry_repository.hangar.name" in writer and "artifactregistry.writer" in writer
    staging = re.search(r'resource "google_storage_bucket_iam_member" "build_staging" \{(.*?)\n\}', main, re.S)
    assert "google_storage_bucket.build_staging.name" in staging.group(1)
    for tf in (REPO / "deploy/terraform").rglob("*.tf"):
        for block in re.findall(r'resource "google_project_iam_member"[^{]*\{(.*?)\n\}', tf.read_text(), re.S):
            if "sa_email" in block:
                assert "storage.admin" not in block and "artifactregistry.writer" not in block, tf


def test_gke_needs_its_tools_and_tfvars(gke, monkeypatch, tmp_path):
    (setup_gke.checkout() / "deploy/terraform/gke/terraform.tfvars").unlink()
    r = runner.invoke(cli.app, ["up", "--gke"])
    assert r.exit_code == 2 and "terraform.tfvars.example" in r.output
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    r = runner.invoke(cli.app, ["up", "--gke"])
    assert r.exit_code == 2 and "missing terraform, gcloud, kubectl" in r.output
    assert runner.invoke(cli.app, ["up", "--dry-run"]).exit_code == 2  # --dry-run is a --gke option


# --------------------------------------------------------------------------- KAN-180: cluster hardening
TF_GKE = REPO / "deploy/terraform/gke"


def _tf() -> str:
    return "\n".join(p.read_text() for p in sorted(TF_GKE.glob("*.tf")))


def _block(text: str, header: str) -> str:
    """The body of the block that starts with `header {`, by brace matching."""
    start = text.index(header)
    i, depth = text.index("{", start), 0
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i + 1:j]
    raise AssertionError(header)


def _defaults() -> dict[str, str]:
    import re

    out = {}
    for name in re.findall(r'^variable "(\w+)"', _tf(), re.M):
        m = re.search(r"^\s*default\s*=\s*(.+)$", _block(_tf(), f'variable "{name}"'), re.M)
        out[name] = m.group(1).strip() if m else None
    return out


def test_every_variable_terraform_uses_is_declared():
    """A cheap stand-in for `terraform validate` where terraform isn't installed (test_alerting runs the real one)."""
    import re

    used = set(re.findall(r"\bvar\.(\w+)", _tf()))
    assert used <= set(_defaults()), used - set(_defaults())


def test_the_provider_supports_in_transit_encryption():
    import re

    pin = re.search(r'^\s+version\s*=\s*">=\s*(\d+)\.(\d+)"', (TF_GKE / "versions.tf").read_text(), re.M)
    assert (int(pin.group(1)), int(pin.group(2))) >= (6, 36)  # in_transit_encryption_config arrived in 6.36.0


def test_cluster_hardening_defaults_are_secure():
    d = _defaults()
    assert d["deletion_protection"] == "true"
    assert d["inter_node_encryption"] == "true"  # KAN-174 L-10
    assert d["secrets_encryption"] == "true"
    assert d["security_posture"] == '"BASIC"'
    assert d["image_vulnerability_scanning"] == "true"
    # Opt-in: they change how up.sh, CI and the power jobs reach the cluster and what nodes can reach.
    assert d["private_nodes"] == "false" and d["master_authorized_networks"] == "[]"
    # Secure Boot forces a new pool (shielded_instance_config is ForceNew): off for existing installs, on in the
    # example new installs copy.
    assert d["shielded_secure_boot"] == "false"
    assert "shielded_secure_boot = true" in (TF_GKE / "terraform.tfvars.example").read_text()

    cluster = _block(_tf(), 'resource "google_container_cluster" "hangar"')
    assert "deletion_protection = var.deletion_protection" in cluster
    assert '"IN_TRANSIT_ENCRYPTION_INTER_NODE_TRANSPARENT"' in cluster
    assert 'datapath_provider = "ADVANCED_DATAPATH"' in cluster  # inter-node encryption needs Dataplane V2
    enc = _block(cluster, 'dynamic "database_encryption"')
    assert 'state    = "ENCRYPTED"' in enc and "local.secrets_key" in enc
    assert "enable_shielded_nodes = true" in cluster
    assert "mode = var.security_posture" in _block(cluster, "security_posture_config")
    assert "enable_private_nodes = true" in _block(cluster, 'dynamic "private_cluster_config"')
    assert "var.master_authorized_networks" in _block(cluster, 'dynamic "master_authorized_networks_config"')
    assert "master_ipv4_cidr_block" not in cluster  # ForceNew; PSC-based clusters don't need it


def test_the_secrets_key_is_hangars_and_only_gke_uses_it():
    tf = _tf()
    key = _block(tf, 'resource "google_kms_crypto_key" "gke_secrets"')
    assert 'rotation_period = "7776000s"' in key
    grant = _block(tf, 'resource "google_kms_crypto_key_iam_member" "gke_secrets"')
    assert "roles/cloudkms.cryptoKeyEncrypterDecrypter" in grant and "container-engine-robot" in grant
    assert 'location   = var.region' in _block(tf, 'resource "google_kms_key_ring" "gke"')  # zonal: the zone's region
    assert "containerscanning.googleapis.com" in _block(tf, 'resource "google_project_service" "hardening"')


def test_every_node_pool_is_shielded_and_on_the_gke_metadata_server():
    import re

    tf = _tf()
    pools = re.findall(r'resource "google_container_node_pool" "(\w+)"', tf)
    assert {"system", "sandboxes", "kata", "heavy", "microvm", "overnight"} <= set(pools), pools
    for name in pools:
        pool = _block(tf, f'resource "google_container_node_pool" "{name}"')
        meta = _block(pool, "workload_metadata_config")
        assert 'mode = "GKE_METADATA"' in meta, name
        shield = _block(pool, "shielded_instance_config")
        assert "enable_secure_boot          = var.shielded_secure_boot" in shield, name
        assert "enable_integrity_monitoring = true" in shield, name
        assert "for_each = var.private_nodes" in _block(pool, 'dynamic "network_config"'), name


def test_private_nodes_get_cloud_nat():
    tf = _tf()
    nat = _block(tf, 'resource "google_compute_router_nat" "nat"')
    assert "var.private_nodes ? 1 : 0" in nat and "ALL_SUBNETWORKS_ALL_IP_RANGES" in nat


def test_the_hardening_is_documented():
    page = (REPO / "docs/security/gke-cluster.md").read_text()
    for control in ("5.3.1", "5.4.2", "5.5.5", "5.5.6", "5.5.7", "5.6.3", "5.6.5", "5.10.4", "5.10.5"):
        assert f"| {control} " in page, control
    assert "Binary Authorization" in page and "L-10" in page
    assert "security/gke-cluster.md" in (REPO / "docs/gke.md").read_text()
    assert "gke-cluster.md" in (REPO / "docs/security/README.md").read_text()
    assert "security/gke-cluster.md" in (REPO / "mkdocs.yml").read_text()
