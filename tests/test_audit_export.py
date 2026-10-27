"""Every audit entry leaves the pod as one `hangar.audit: {json}` line, and the Terraform routes those lines to a log
bucket hangar can't rewrite (KAN-182; docs/security/audit.md)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from hangar.models import AuditEvent
from hangar.store import Store

ROOT = Path(__file__).resolve().parents[1]
AUDIT_TF = (ROOT / "deploy/terraform/gke/audit.tf").read_text()


def test_each_entry_is_one_line_of_the_json_the_table_holds(tmp_path, caplog):
    store = Store(tmp_path / "hangar.db")
    event = AuditEvent(actor="admin", action="job.approve", target="job-1",
                       detail={"note": "two\nlines", "rule": "policy"})
    with caplog.at_level(logging.INFO, logger="hangar.audit"):
        store.audit(event)
    records = [r for r in caplog.records if r.name == "hangar.audit"]
    assert len(records) == 1
    line = records[0].getMessage()
    assert "\n" not in line
    assert json.loads(line) == json.loads(event.model_dump_json())
    assert store.audit_tail(1)[0] == event  # the table still holds it


def test_the_log_format_puts_the_logger_name_where_the_sink_filter_looks():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")  # cli.py serve
    rec = logging.LogRecord("hangar.audit", logging.INFO, __file__, 1, '{"action": "x"}', None, None)
    assert 'hangar.audit: {"action"' in fmt.format(rec)
    assert 'textPayload:\\"hangar.audit: {\\"' in AUDIT_TF


def test_the_sink_goes_to_a_retained_bucket_and_hangar_cannot_administer_it():
    assert re.search(r'resource "google_logging_project_bucket_config" "audit"', AUDIT_TF)
    assert re.search(r"retention_days\s*=\s*var\.audit_retention_days", AUDIT_TF)
    assert re.search(r"locked\s*=\s*var\.audit_bucket_locked", AUDIT_TF)
    assert re.search(r'resource "google_logging_project_sink" "audit"', AUDIT_TF)
    assert "unique_writer_identity" in AUDIT_TF
    # hangar's own identity only writes logs: no logging admin or config role anywhere in its Terraform.
    for tf in (ROOT / "deploy/terraform/gke").glob("*.tf"):
        assert "roles/logging.admin" not in tf.read_text(), tf.name
        assert "roles/logging.configWriter" not in tf.read_text(), tf.name
