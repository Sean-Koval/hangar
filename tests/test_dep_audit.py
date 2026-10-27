"""The dependency audit's gate (KAN-179): High or Critical with a fix fails; anything else is reported."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("audit_deps", ROOT / "scripts" / "ci" / "audit_deps.py")
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def _vuln(vid: str, fixed: str | None = None, label: str | None = None, name: str = "pkg") -> dict:
    events = [{"introduced": "0"}] + ([{"fixed": fixed}] if fixed else [])
    v = {"id": vid, "affected": [{"package": {"name": name, "ecosystem": "PyPI"}, "ranges": [{"events": events}]}]}
    if label:
        v["database_specific"] = {"severity": label}
    return v


def _report(*groups: tuple[str, list[dict], str]) -> dict:
    pkgs = [{"package": {"name": name, "version": "1.0"}, "vulnerabilities": vulns,
             "groups": [{"ids": [v["id"] for v in vulns], "max_severity": score}]} for name, vulns, score in groups]
    return {"results": [{"packages": pkgs}]}


def test_fixable_high_or_critical_fails_and_the_rest_is_reported(tmp_path, capsys):
    report = _report(("crit", [_vuln("A", "1.1", name="crit")], "9.1"),
                     ("high-nofix", [_vuln("B", name="high-nofix")], "7.5"),
                     ("medium", [_vuln("C", "2.0", name="medium")], "5.0"),
                     ("labelled", [_vuln("D", "3.0", "HIGH", name="labelled")], ""),
                     ("unknown", [_vuln("E", "3.0", name="unknown")], ""))
    rows = {r["package"]: r for r in audit.findings(report)}
    assert {k: (r["severity"], r["fails"]) for k, r in rows.items()} == {
        "crit": ("Critical", True), "high-nofix": ("High", False), "medium": ("Medium", False),
        "labelled": ("High", True), "unknown": ("Unknown", False)}
    p = tmp_path / "osv.json"
    p.write_text(json.dumps(report))
    assert audit.main(["audit_deps.py", str(p)]) == 1
    assert "2 High or Critical finding(s) with a fix" in capsys.readouterr().out


def test_a_clean_or_empty_report_passes(tmp_path):
    p = tmp_path / "osv.json"
    for text in ("", json.dumps({"results": []}), json.dumps(_report(("low", [_vuln("F", "1", name="low")], "2.0")))):
        p.write_text(text)
        assert audit.main(["audit_deps.py", str(p)]) == 0


def test_the_scans_upload_where_they_have_permission():
    """Every job that runs the Trivy action can upload SARIF, and only those and CodeQL ask for security-events."""
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        for name, job in (yaml.safe_load(wf.read_text())["jobs"] or {}).items():
            uses = [str(s.get("uses", "")) for s in job.get("steps", [])]
            uploads = any(u == "./.github/actions/trivy-image" or u.startswith("github/codeql-action/") for u in uses)
            asks = (job.get("permissions") or {}).get("security-events") == "write"
            calls_images = job.get("uses") == "./.github/workflows/images.yml"
            assert asks == (uploads or calls_images), f"{wf.name}:{name}"
