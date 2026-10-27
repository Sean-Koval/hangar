"""The control-plane image's apt layer: pinned versions, a cache-bust that release and main builds pass, and the
weekly check that reports stale pins (KAN-185)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
WORKFLOWS = ROOT / ".github" / "workflows"

_spec = importlib.util.spec_from_file_location("apt_versions", ROOT / "scripts" / "ci" / "apt_versions.py")
apt = sys.modules.setdefault("apt_versions", importlib.util.module_from_spec(_spec))
_spec.loader.exec_module(apt)


def _apt_run() -> str:
    return next(r for r in re.findall(r"^RUN .*?(?<!\\)\n", DOCKERFILE, re.M | re.S) if "apt-get" in r)


def test_every_apt_package_is_installed_at_its_pinned_version():
    pins = apt.pinned(DOCKERFILE)
    run = _apt_run()
    for p in apt.PINS:
        assert p.arg in pins, f"pin {p.package} with ARG {p.arg}=<version>"
        assert f'"{p.package}=${p.arg}"' in run, f"install {p.package} as \"{p.package}=${p.arg}\""
    assert apt.codename(pins["DOCKER_CLI_VERSION"]) == apt.codename(pins["DOCKER_BUILDX_VERSION"]) != ""


def test_the_apt_layer_uses_the_refresh_arg():
    assert re.search(r"^ARG APT_REFRESH=", DOCKERFILE, re.M)
    assert "$APT_REFRESH" in _apt_run()


@pytest.mark.parametrize(("workflow", "value"), [
    ("release.yml", "APT_REFRESH=${{ env.TAG }}-${{ github.run_id }}"),
    ("deploy.yml", "APT_REFRESH=${{ steps.apt.outputs.week }}"),
    ("ci.yml", "APT_REFRESH=${{ steps.apt.outputs.week }}"),
])
def test_release_and_main_builds_bust_the_apt_cache(workflow, value):
    text = (WORKFLOWS / workflow).read_text()
    assert value in text
    if "week" in value:
        assert "date -u +%G-W%V" in text


def test_the_weekly_check_is_scheduled_with_least_privilege():
    text = (WORKFLOWS / "apt-versions.yml").read_text()
    assert re.search(r"^\s+- cron:", text, re.M) and "scripts/ci/apt_versions.py" in text
    assert re.search(r"^permissions:\n  contents: read\n", text, re.M)
    granted = set(re.findall(r"^\s+(\w[\w-]*): write", text, re.M))
    assert granted == {"issues"}, granted


@pytest.mark.parametrize(("a", "b", "want"), [
    ("590.0.1-0", "590.0.0-0", 1),
    ("590.0.1-0", "590.0.1-0", 0),
    ("589.0.0-0", "590.0.0-0", -1),
    ("5:29.9.2-1~debian.13~trixie", "5:29.10.0-1~debian.13~trixie", -1),
    ("5:29.9.2-1~debian.13~trixie", "29.9.9-1~debian.13~trixie", 1),  # the epoch wins
    ("0.38.1-1~debian.13~trixie", "0.38.1-1", -1),  # ~ sorts before everything
    ("1.0~rc1-1", "1.0-1", -1),
])
def test_debian_version_order(a, b, want):
    assert apt.compare(a, b) == want
    assert apt.compare(b, a) == -want


INDEX = """Package: docker-ce-cli
Version: 5:29.9.2-1~debian.13~trixie

Package: docker-ce-cli
Version: 5:29.10.0-1~debian.13~trixie

Package: docker-buildx-plugin
Version: 0.38.1-1~debian.13~trixie

Package: google-cloud-cli
Version: 590.0.1-0

Package: google-cloud-cli
Version: 589.0.0-0
"""

FAKE = """ARG DOCKER_CLI_VERSION=5:29.9.2-1~debian.13~trixie
ARG DOCKER_BUILDX_VERSION=0.38.0-1~debian.13~trixie
ARG GCLOUD_VERSION=590.0.1-0
"""


def test_check_reports_each_pin_and_write_bumps_the_stale_ones():
    urls = []
    rows = apt.check(FAKE, fetch=lambda url: urls.append(url) or INDEX)
    assert {r["package"]: r["status"] for r in rows} == {
        "docker-ce-cli": "behind", "docker-buildx-plugin": "missing", "google-cloud-cli": "current"}
    assert any("/dists/trixie/" in u for u in urls)
    out = apt.write(FAKE, rows)
    assert "ARG DOCKER_CLI_VERSION=5:29.10.0-1~debian.13~trixie\n" in out
    assert "ARG DOCKER_BUILDX_VERSION=0.38.1-1~debian.13~trixie\n" in out
    assert "ARG GCLOUD_VERSION=590.0.1-0\n" in out
    assert "| docker-ce-cli |" in apt.markdown(rows)
