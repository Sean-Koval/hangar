"""Are the control-plane image's apt pins current? (KAN-185)

The Dockerfile pins docker-ce-cli, docker-buildx-plugin and google-cloud-cli in ARG lines. This reads each package's
apt repository index and compares. Dependabot can't bump apt, so the weekly apt-versions workflow runs this and opens
or updates an issue when a pin falls behind.

    python scripts/ci/apt_versions.py                 # report; exit 0 when current, 3 when a pin is behind
    python scripts/ci/apt_versions.py --write         # bump the ARG lines in the Dockerfile to the latest
    python scripts/ci/apt_versions.py --markdown F    # also write the report to F (the issue body)

It downloads the indexes only; nothing from the repositories runs.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
DOCKER_INDEX = "https://download.docker.com/linux/debian/dists/{codename}/stable/binary-amd64/Packages"
GCLOUD_INDEX = "https://packages.cloud.google.com/apt/dists/cloud-sdk/main/binary-amd64/Packages"
BEHIND = 3


@dataclass(frozen=True)
class Pin:
    arg: str
    package: str
    index: str  # a format string; {codename} comes from the pinned version


PINS = (
    Pin("DOCKER_CLI_VERSION", "docker-ce-cli", DOCKER_INDEX),
    Pin("DOCKER_BUILDX_VERSION", "docker-buildx-plugin", DOCKER_INDEX),
    Pin("GCLOUD_VERSION", "google-cloud-cli", GCLOUD_INDEX),
)


# --- Debian version order (deb-version(7)): epoch, then upstream and revision, compared part by part.
def _order(c: str) -> int:
    if c == "~":
        return -1
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _compare_part(a: str, b: str) -> int:
    while a or b:
        na, nb = re.match(r"\D*", a).group(), re.match(r"\D*", b).group()
        for i in range(max(len(na), len(nb))):
            ca, cb = (_order(na[i]) if i < len(na) else 0), (_order(nb[i]) if i < len(nb) else 0)
            if ca != cb:
                return -1 if ca < cb else 1
        a, b = a[len(na):], b[len(nb):]
        da, db = re.match(r"\d*", a).group(), re.match(r"\d*", b).group()
        if int(da or 0) != int(db or 0):
            return -1 if int(da or 0) < int(db or 0) else 1
        a, b = a[len(da):], b[len(db):]
    return 0


def _split(v: str) -> tuple[int, str, str]:
    epoch, _, rest = v.rpartition(":") if ":" in v else ("0", "", v)
    upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "0")
    return int(epoch), upstream, revision


def compare(a: str, b: str) -> int:
    """-1, 0 or 1 as Debian version a sorts before, with or after b."""
    ea, ua, ra = _split(a)
    eb, ub, rb = _split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _compare_part(ua, ub) or _compare_part(ra, rb)


def versions(index_text: str, package: str) -> list[str]:
    """Every Version of `package` in an apt Packages index."""
    out, current = [], None
    for line in index_text.splitlines():
        if line.startswith("Package:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("Version:") and current == package:
            out.append(line.split(":", 1)[1].strip())
    return out


def latest(candidates: list[str]) -> str | None:
    best = None
    for v in candidates:
        if best is None or compare(v, best) > 0:
            best = v
    return best


def pinned(dockerfile: str) -> dict[str, str]:
    return dict(re.findall(r"^ARG\s+(\w+_VERSION)=(\S+)", dockerfile, re.M))


def codename(version: str) -> str:
    """The Debian release a Docker package version names: 5:29.9.2-1~debian.13~trixie -> trixie."""
    m = re.search(r"~debian\.\d+~(\w+)$", version)
    return m.group(1) if m else ""


def _fetch(url: str, cache: dict[str, str]) -> str:
    if url not in cache:
        with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - fixed https URLs above
            cache[url] = r.read().decode()
    return cache[url]


def check(dockerfile: str, fetch=None) -> list[dict]:
    """One row per pin: arg, package, pinned, latest, status (current, behind, missing or unpinned)."""
    cache: dict[str, str] = {}
    fetch = fetch or (lambda url: _fetch(url, cache))
    pins, rows = pinned(dockerfile), []
    for p in PINS:
        have = pins.get(p.arg)
        if not have:
            rows.append({"arg": p.arg, "package": p.package, "pinned": None, "latest": None, "status": "unpinned"})
            continue
        found = versions(fetch(p.index.format(codename=codename(have))), p.package)
        new = latest(found)
        if have not in found:
            status = "missing"  # gone from the repo: the build would fail
        elif new and compare(new, have) > 0:
            status = "behind"
        else:
            status = "current"
        rows.append({"arg": p.arg, "package": p.package, "pinned": have, "latest": new, "status": status})
    return rows


def write(dockerfile: str, rows: list[dict]) -> str:
    for r in rows:
        if r["latest"] and r["status"] in ("behind", "missing"):
            dockerfile = re.sub(rf"^(ARG\s+{r['arg']}=)\S+", lambda m, v=r["latest"]: m.group(1) + v, dockerfile,
                                flags=re.M)
    return dockerfile


def markdown(rows: list[dict]) -> str:
    lines = [
        "The control-plane image's apt pins (Dockerfile) against their apt repositories.",
        "",
        "| package | pinned | latest | status |",
        "|---|---|---|---|",
    ]
    lines += [f"| {r['package']} | `{r['pinned']}` | `{r['latest']}` | {r['status']} |" for r in rows]
    lines += [
        "",
        "To bump: `python scripts/ci/apt_versions.py --write`, then open a PR with the Dockerfile change. "
        "Release notes: [Docker CLI](https://docs.docker.com/engine/release-notes/), "
        "[Buildx](https://github.com/docker/buildx/releases), "
        "[gcloud](https://cloud.google.com/sdk/docs/release-notes).",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dockerfile", type=Path, default=DOCKERFILE)
    ap.add_argument("--write", action="store_true", help="bump the ARG lines to the latest versions")
    ap.add_argument("--markdown", type=Path, help="write the report as Markdown to this file")
    args = ap.parse_args(argv)
    text = args.dockerfile.read_text()
    rows = check(text)
    for r in rows:
        print(f"{r['package']:<22} {r['pinned'] or '-':<32} {r['latest'] or '-':<32} {r['status']}")
    if args.markdown:
        args.markdown.write_text(markdown(rows))
    stale = [r for r in rows if r["status"] != "current"]
    if args.write and stale:
        args.dockerfile.write_text(write(text, rows))
        print(f"updated {args.dockerfile}")
        return 0
    return BEHIND if stale else 0


if __name__ == "__main__":
    sys.exit(main())
