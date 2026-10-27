#!/usr/bin/env python3
"""Gate on osv-scanner's audit of uv.lock (KAN-179, docs/security/operations.md#vulnerability-management).

    osv-scanner scan source --lockfile uv.lock --format json > osv.json || true
    scripts/ci/audit_deps.py osv.json          # a Markdown table on stdout; exit 1 on a fixable High or Critical

A finding fails the check when its severity is High or Critical and a fixed version exists. The severity is the
highest CVSS score osv-scanner reports for the group of aliases (9.0 and up Critical, 7.0 High, 4.0 Medium), or the
GitHub advisory's own label when there is no score. A finding with neither is Unknown and only reported. Findings
recorded in osv-scanner.toml, each with its reason, never reach this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ORDER = ["Critical", "High", "Medium", "Low", "Unknown"]
GHSA_LABELS = {"CRITICAL": "Critical", "HIGH": "High", "MODERATE": "Medium", "MEDIUM": "Medium", "LOW": "Low"}


def severity(score: str | None, vulns: list[dict]) -> str:
    try:
        s = float(score or "")
    except ValueError:
        s = None
    if s is not None:
        return "Critical" if s >= 9 else "High" if s >= 7 else "Medium" if s >= 4 else "Low"
    labels = [GHSA_LABELS.get(str((v.get("database_specific") or {}).get("severity", "")).upper()) for v in vulns]
    known = [x for x in labels if x]
    return min(known, key=ORDER.index) if known else "Unknown"


def fixed_versions(name: str, vulns: list[dict]) -> list[str]:
    out = set()
    for v in vulns:
        for a in v.get("affected", []):
            if (a.get("package") or {}).get("name", "").lower() != name.lower():
                continue
            for r in a.get("ranges", []):
                out.update(e["fixed"] for e in r.get("events", []) if "fixed" in e)
    return sorted(out)


def findings(report: dict) -> list[dict]:
    rows = []
    for result in report.get("results") or []:
        for pkg in result.get("packages") or []:
            name, version = pkg["package"]["name"], pkg["package"].get("version", "")
            by_id = {v["id"]: v for v in pkg.get("vulnerabilities") or []}
            for group in pkg.get("groups") or []:
                vulns = [by_id[i] for i in group.get("ids", []) if i in by_id]
                sev = severity(group.get("max_severity"), vulns)
                fixes = fixed_versions(name, vulns)
                rows.append({"package": name, "version": version, "ids": group.get("ids", []), "severity": sev,
                             "fixed": fixes, "fails": sev in ("Critical", "High") and bool(fixes)})
    return sorted(rows, key=lambda r: (ORDER.index(r["severity"]), r["package"]))


def main(argv: list[str]) -> int:
    rows = findings(json.loads(Path(argv[1]).read_text() or "{}"))
    if not rows:
        print("osv-scanner: no known vulnerabilities in uv.lock.")
        return 0
    print("| severity | package | ids | fixed in | gate |\n|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['severity']} | {r['package']} {r['version']} | {', '.join(r['ids'])} "
              f"| {', '.join(r['fixed']) or 'no fix yet'} | {'**fails**' if r['fails'] else 'reported'} |")
    failing = [r for r in rows if r["fails"]]
    if failing:
        print(f"\n{len(failing)} High or Critical finding(s) with a fix: bump them (`uv lock --upgrade-package "
              "NAME`), or record each in osv-scanner.toml with its reason.")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
