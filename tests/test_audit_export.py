"""Every audit entry leaves the pod as one `hangar.audit: {json}` line, and the Terraform routes those lines to a log
bucket hangar can't rewrite (KAN-182; docs/security/audit.md)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from hangar import logfmt
from hangar.models import AuditEvent
from hangar.store import Store

ROOT = Path(__file__).resolve().parents[1]
AUDIT_TF = (ROOT / "deploy/terraform/gke/audit.tf").read_text()


def test_each_entry_is_one_line_of_the_json_the_table_holds(tmp_path, caplog):
    store = Store(tmp_path / "hangar.db")
    store.set_audit_key(b"k" * 32)
    event = AuditEvent(actor="admin", action="job.approve", target="job-1",
                       detail={"note": "two\nlines", "rule": "policy"})
    with caplog.at_level(logging.INFO, logger="hangar.audit"):
        store.audit(event)
    records = [r for r in caplog.records if r.name == "hangar.audit"]
    assert len(records) == 1
    line = records[0].getMessage()
    assert "\n" not in line
    logged = json.loads(line)
    # --- KAN-296: the table's JSON, then the row's place in the hash chain, so a gap in the table shows off the pod
    seq, link = logged.pop("seq"), logged.pop("hash")
    assert logged == json.loads(event.model_dump_json())
    assert (seq, link) == store.audit_rows()[-1][::4][:2] and link
    assert store.audit_tail(1)[0] == event  # the table still holds it


def sink_regex(tf: str) -> str:
    """The audit sink's textPayload regex, as Terraform has it: the sink's filter, and the read command it prints
    (--- KAN-556: anchored the same way, so a forged entry already in the bucket doesn't count when read)."""
    found = re.findall(r'textPayload=~\\"(.*?)\\"', tf)
    assert len(found) == 2 and len(set(found)) == 1, found
    return found[0]


def test_the_log_format_puts_the_logger_name_where_the_sink_filter_looks():
    fmt = logfmt.OneEntry()  # cli.py serve
    rec = logging.LogRecord("hangar.audit", logging.INFO, __file__, 1, '{"action": "x"}', None, None)
    assert logfmt.AUDIT_LINE.match(fmt.format(rec))
    assert sink_regex(AUDIT_TF) == logfmt.AUDIT_LINE.pattern
    other = logging.LogRecord("hangar.jobs", logging.INFO, __file__, 1, 'a title hangar.audit: {"action": "x"}', None,
                              None)
    assert not logfmt.AUDIT_LINE.match(fmt.format(other))  # --- KAN-556: anywhere but the start doesn't count


def test_text_from_a_sandbox_cannot_start_a_log_line_that_looks_like_an_audit_record():
    """--- KAN-556: a record quoting a sandbox's output (canaries.py, experiments.py log a failed command's stderr)
    used to put that output's lines in the log as lines of their own, and the substring filter kept a forged one."""
    fmt = logfmt.OneEntry()
    forged = '2026-11-18 03:00:00,123 INFO hangar.audit: {"actor": "admin", "action": "job.approve", "seq": 9}'
    rec = logging.LogRecord("hangar.canaries", logging.WARNING, __file__, 1, "could not plant: %s", (
        f"boom\n{forged}\r{forged}\r\n{forged}",), None)
    try:
        raise RuntimeError(f"stderr:\n{forged}")
    except RuntimeError:
        import sys

        rec.exc_info = sys.exc_info()
    lines = fmt.format(rec).split("\n")
    assert len(lines) > 4 and all(line.startswith(logfmt.CONTINUED) for line in lines[1:])
    assert not any(logfmt.AUDIT_LINE.match(line) for line in lines)
    assert not any(re.match(sink_regex(AUDIT_TF), line.lstrip()) for line in lines[1:])  # whitespace or not


def test_a_long_line_is_cut_before_the_runtime_splits_it():
    """--- KAN-556: the container runtime makes a line over 16 KiB several entries; padding sandbox text to that edge
    could start one with a forged record. OneEntry cuts it first, each piece continued, never mid-character."""
    fmt = logfmt.OneEntry()
    forged = '2026-11-18 03:00:00,123 INFO hangar.audit: {"actor": "admin", "action": "job.approve", "seq": 9}'
    for pad in ("x", "é", "日", "😀", "\ud800"):  # a lone surrogate: 6 bytes on stderr (backslashreplace)
        for edge in (16 * 1024, logfmt.PIECE, logfmt.PIECE - 1, 2 * logfmt.PIECE + 3):
            text = pad * (edge // len(pad.encode("utf-8", "backslashreplace"))) + forged + "y" * 40_000
            rec = logging.LogRecord("hangar.canaries", logging.WARNING, __file__, 1, "%s", (text,), None)
            out = fmt.format(rec)
            lines = out.split("\n")
            sizes = [len(ln.encode("utf-8", "backslashreplace")) for ln in lines]
            assert len(lines) > 2 and max(sizes) <= logfmt.PIECE + len(logfmt.CONTINUED)
            assert all(ln.startswith(logfmt.CONTINUED) for ln in lines[1:])
            assert "".join([lines[0], *(ln[len(logfmt.CONTINUED):] for ln in lines[1:])]).endswith(text)
            assert not any(logfmt.AUDIT_LINE.match(ln) for ln in lines[1:])
    short = logging.LogRecord("hangar.audit", logging.INFO, __file__, 1, '{"seq": 1}', None, None)
    assert "\n" not in fmt.format(short)


def test_serve_logs_through_one_entry_lines(monkeypatch, tmp_path):
    import hangar.cli as cli
    from hangar.config import get_settings

    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: seen.update(kw))
    import sys
    import threading
    import warnings

    root = logging.getLogger()
    before = (root.handlers[:], root.level)
    hooks = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    monkeypatch.setenv("HANGAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()  # serve caches the settings it reads; later tests read their own
    try:
        from typer.testing import CliRunner

        assert CliRunner().invoke(cli.app, ["serve"]).exit_code == 0
        assert seen["log_config"] is None  # uvicorn's loggers propagate to the root handler
        assert root.handlers and all(isinstance(h.formatter, logfmt.OneEntry) for h in root.handlers)
        assert sys.excepthook is not hooks[0] and threading.excepthook is logfmt._thread_exception
        assert sys.unraisablehook is not hooks[2]
        assert warnings.showwarning is logging._showwarning  # type: ignore[attr-defined]  # warnings: logged
    finally:
        root.handlers[:], _ = before[0], root.setLevel(before[1])
        sys.excepthook, threading.excepthook, sys.unraisablehook = hooks
        logging.captureWarnings(False)
        get_settings.cache_clear()


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
