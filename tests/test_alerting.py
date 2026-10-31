"""KAN-144: the SLO alerting is well formed (deploy/terraform/gke/alerting.tf, deploy/k8s/observability/
prometheus-rules.yaml, deploy/gke/fire-test-alerts.sh, docs/slos.md, docs/alerting.md).

The log-based metrics match the lines hangar really writes (src/hangar/slolog.py), every alert names a runbook that
exists and is in the docs' table, every log-based alert can be fired on purpose, the Prometheus rules read metrics
hangar exports, everything is opt-in, and terraform fmt and validate pass where terraform is installed."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from hangar import metrics, slolog
from hangar.models import Job, JobEvent, JobState

ROOT = Path(__file__).resolve().parents[1]
TF = ROOT / "deploy" / "terraform" / "gke"
ALERTING = (TF / "alerting.tf").read_text()
RULES = ROOT / "deploy" / "k8s" / "observability" / "prometheus-rules.yaml"
FIRE = ROOT / "deploy" / "gke" / "fire-test-alerts.sh"
DOCS = ROOT / "docs"


def _hcl_unquote(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _metric_blocks() -> dict[str, str]:
    """name -> the body of each google_logging_metric resource."""
    out = {}
    for m in re.finditer(r'resource "google_logging_metric" "\w+" \{(.*?)\n\}', ALERTING, re.S):
        out[re.search(r'name\s+=\s+"(\w+)"', m.group(1)).group(1)] = m.group(1)
    return out


def _filter_needles(body: str) -> list[str]:
    """The textPayload substrings a metric's filter asks for."""
    f = re.search(r'filter\s+=\s+"(.*)"\n', body).group(1)
    return [_hcl_unquote(x) for x in re.findall(r'textPayload:\\"(.*?)\\"', f)]


def _extractors(body: str) -> dict[str, str]:
    """label or value -> its regular expression."""
    out = {k: _hcl_unquote(v) for k, v in re.findall(r'(\w+)\s+=\s+"REGEXP_EXTRACT\(textPayload, \\"(.*?)\\"\)"', body)}
    return out


def _real_lines(caplog) -> dict[str, str]:
    """One real line per event, as hangar writes it (with the `hangar.slo: ` the log formatter puts in front)."""
    caplog.set_level(logging.INFO, "hangar.slo")
    metrics.setup(False)
    job = Job(title="t", instructions="x", environment="base", created_at=0)
    job.events = [JobEvent(ts=0, state="queued"), JobEvent(ts=5, state="provisioning")]
    slolog.job_transition(job, "provisioning", "running", at=12.5)
    job.reason, job.state = "error", JobState.FAILED
    slolog.job_transition(job, "provisioning", "failed", at=12.5)
    slolog.sandbox_create("kubernetes", "base", 3.5, RuntimeError("x"))
    slolog.fail_closed("hangar.tool.call", "unreachable")
    slolog.emit("jobs_stuck", n=1, oldest_s=4000, states="queued")
    slolog.emit("deploy_lag", commits=383, working_days=8, days=12)  # --- KAN-216 (deploylag.tick)
    lines = ["hangar.slo: " + r.getMessage() for r in caplog.records if r.name == "hangar.slo"]
    return {"job_start_ok": lines[0], "job_start_error": lines[1], "sandbox_create": lines[2],
            "fail_closed": lines[3], "jobs_stuck": lines[4], "deploy_lag": lines[5]}


def _matches(body: str, line: str) -> bool:
    return all(n in line for n in _filter_needles(body))


def test_every_log_metric_matches_a_real_line_and_extracts_from_it(caplog):
    lines = _real_lines(caplog)
    blocks = _metric_blocks()
    assert set(blocks) == {"hangar_job_starts", "hangar_job_queue_seconds", "hangar_sandbox_creates",
                           "hangar_fail_closed", "hangar_jobs_stuck", "hangar_deploy_lag"}
    want = {"hangar_job_starts": ("job_start_ok", {"result": "ok"}),
            "hangar_job_queue_seconds": ("job_start_ok", {"value": "12.5"}),
            "hangar_sandbox_creates": ("sandbox_create", {"result": "error", "provider": "kubernetes"}),
            "hangar_fail_closed": ("fail_closed", {"failure": "unreachable"}),
            "hangar_jobs_stuck": ("jobs_stuck", {}),
            "hangar_deploy_lag": ("deploy_lag", {})}
    for name, (which, values) in want.items():
        body, line = blocks[name], lines[which]
        assert _matches(body, line), (name, line)
        for key, regex in _extractors(body).items():
            key = "value" if key == "value_extractor" else key
            assert re.search(regex, line).group(1) == values[key], (name, key)
    # the queue SLO counts only ok starts; a failed one isn't a wait that ended
    assert not _matches(blocks["hangar_job_queue_seconds"], lines["job_start_error"])
    assert _matches(blocks["hangar_job_starts"], lines["job_start_error"])
    assert re.search(_extractors(blocks["hangar_job_starts"])["result"], lines["job_start_error"]).group(1) == "error"
    # a held start (the night, a review cap) is left out of the queue SLO
    assert not _matches(blocks["hangar_job_queue_seconds"], lines["job_start_ok"].replace("held=no", "held=yes"))


def test_metrics_count_hangars_container_and_the_test_log_only():
    assert 'resource.labels.container_name=\\"hangar\\"' in ALERTING
    assert 'logs/hangar-slo-test' in ALERTING and "LOG=hangar-slo-test" in FIRE.read_text()


def _policies() -> dict[str, dict[str, str]]:
    block = re.search(r"promql_policies = \{(.*?)\n  \}\n", ALERTING, re.S).group(1)
    out = {}
    for m in re.finditer(r"\n    ([\w-]+) = \{(.*?)\n    \}", block, re.S):
        fields = dict(re.findall(r'(\w+)\s+= "(.*)"', m.group(2)))
        out[m.group(1)] = fields
    return out


def _doc_exists(path: str) -> bool:
    page, _, anchor = path.partition("#")
    md = DOCS / (page.rstrip("/") + ".md")
    if not md.exists():
        return False
    if not anchor:
        return True
    slugs = {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-")
             for h in re.findall(r"^#+ (.+)$", md.read_text(), re.M)}
    return anchor in slugs


def test_every_alert_has_a_severity_a_runbook_and_a_row_in_the_docs():
    pol = _policies()
    assert set(pol) == {"job-starts-failing", "job-starts-slow-burn", "queue-slow", "sandbox-creates-failing",
                        "jobs-stuck", "tower-fail-closed", "restarts", "data-disk", "node-disk", "deploy-lag"}
    table = (DOCS / "alerting.md").read_text()
    for key, p in pol.items():
        assert p["severity"] in ("CRITICAL", "WARNING"), key
        assert _doc_exists(p["runbook"]), (key, p["runbook"])
        assert f"| `{key}` |" in table, key
    assert "| `api-down` |" in table and 'display_name          = "hangar api-down"' in ALERTING
    for page in ("runbooks/on-call", "slos"):
        assert f"${{local.docs}}/{page}/" in ALERTING and _doc_exists(page)


def test_every_log_based_alert_can_be_fired_on_purpose(tmp_path):
    out = subprocess.run(["bash", str(FIRE)], env={**os.environ, "DRY_RUN": "1", "PROJECT": "p"}, capture_output=True,
                         text=True, check=True, cwd=tmp_path).stdout
    fired = {n.strip() for names in re.findall(r"^== hangar ([\w, -]+):", out, re.M) for n in names.split(",")}
    log_based = {k for k, p in _policies().items() if "${local.m." in p["query"]}
    assert fired == log_based
    blocks = _metric_blocks()
    written = [ln.replace("\\ ", " ") for ln in re.findall(r"gcloud logging write hangar-slo-test (\S.*?) --project p",
                                                           out)]
    for name in blocks:  # each metric sees at least one test line
        assert any(_matches(blocks[name], ln) for ln in written), name
    assert "unknown alert" in subprocess.run(["bash", str(FIRE), "nope"], env={**os.environ, "DRY_RUN": "1",
                                             "PROJECT": "p"}, capture_output=True, text=True).stderr


def test_everything_is_opt_in_and_routed_to_the_oncall_channels():
    assert re.search(r'variable "enable_alerting" \{[^}]*default\s+= false', ALERTING, re.S)
    for m in re.finditer(r'^resource "([\w]+)" "(\w+)" \{\n(.*?)^\}', ALERTING, re.S | re.M):
        head = m.group(3).split("\n")[0]
        assert re.match(r"\s+(count|for_each)\s+=", head), f"{m.group(1)}.{m.group(2)} isn't opt-in"
        assert "local.alerting" in head or "local.escalate" in head, m.group(2)
    for m in re.finditer(r'resource "google_monitoring_alert_policy" "(\w+)" \{(.*?)\n\}', ALERTING, re.S):
        assert "notification_channels = " in m.group(2) and "documentation {" in m.group(2), m.group(1)
    assert 'output "alert_channels"' in ALERTING and "sensitive   = true" in ALERTING
    assert "alert_slack_token" not in (TF / "terraform.tfvars.example").read_text().split("TF_VAR_")[0].split(
        "enable_alerting")[-1]


def test_the_dashboard_shows_each_slo_with_its_target():
    for tile in ("job_start", "queue", "creates"):
        assert re.search(rf"{tile}\s+= \[\"[^\"]+\(SLO", ALERTING), tile
    assert "prometheusQuery" in ALERTING and 'displayName = "hangar SLOs"' in ALERTING


def test_the_prometheus_rules_read_metrics_hangar_exports():
    doc = yaml.safe_load(RULES.read_text())
    assert doc["kind"] == "PrometheusRule" and doc["apiVersion"] == "monitoring.coreos.com/v1"
    m = metrics.Metrics()
    from prometheus_client import generate_latest

    m.job_starts.labels("ok").inc()
    m.job_queue.observe(1)
    m.fail_closed.labels("k", "unreachable").inc()
    m.sandbox_create.labels("p", "e", "ok").observe(1)
    m.loop_failures.labels("ops").inc()
    m.backup_last.set(1)
    m.jobs_stuck.set(0)
    m.deploy_lag_over.set(0)
    exported = set(re.findall(r"^(hangar_\w+?)(?:\{| )", generate_latest(m.registry).decode(), re.M))
    records = set()
    alerts = []
    for group in doc["spec"]["groups"]:
        for rule in group["rules"]:
            for name in re.findall(r"\b(hangar_\w+)", rule["expr"]):
                assert name in exported, name
            records.update([rule["record"]] if "record" in rule else [])
            alerts.extend([rule] if "alert" in rule else [])
    for a in alerts:
        assert a["labels"]["severity"] in ("page", "ticket"), a["alert"]
        url = a["annotations"]["runbook_url"]
        assert url.startswith("https://sean-koval.github.io/hangar/") and _doc_exists(
            url.removeprefix("https://sean-koval.github.io/hangar/")), url
        for rec in re.findall(r"\b(hangar:[\w:]+)", a["expr"]):
            assert rec in records, rec
    assert {"HangarJobStartsFailing", "HangarQueueSlow", "HangarSandboxCreatesFailing",
            "HangarTowerFailClosed", "HangarJobStuck", "HangarDeployLag"} <= {a["alert"] for a in alerts}


def test_the_runbooks_are_linked_from_the_index_and_the_nav():
    index = (DOCS / "runbooks" / "README.md").read_text()
    nav = (ROOT / "mkdocs.yml").read_text()
    for page in ("on-call", "stuck-job", "credential-leak", "tower-down", "disk-and-quota", "bad-deploy"):
        assert f"]({page}.md)" in index and f"runbooks/{page}.md" in nav, page
    assert "slos.md" in nav and "alerting.md" in nav
    assert "cicd.md#4-when-it-fails-it-rolls-back" in (DOCS / "runbooks" / "bad-deploy.md").read_text()
    assert "sean-koval.github.io/tower/runbooks/tower-down/" in (DOCS / "runbooks" / "tower-down.md").read_text()


def test_terraform_parses():
    hcl2 = pytest.importorskip("hcl2")
    hcl2.loads(ALERTING)


@pytest.mark.skipif(not shutil.which("terraform"), reason="needs terraform")
def test_terraform_fmt_and_validate(tmp_path):
    subprocess.run(["terraform", "fmt", "-check", "-diff", str(TF)], check=True)
    work = tmp_path / "gke"
    shutil.copytree(TF, work, ignore=shutil.ignore_patterns(".terraform*", "*.tfstate*", "terraform.tfvars"))
    if subprocess.run(["terraform", f"-chdir={work}", "init", "-backend=false", "-input=false"],
                      capture_output=True).returncode != 0:
        pytest.skip("terraform init needs the provider download")
    r = subprocess.run(["terraform", f"-chdir={work}", "validate"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
