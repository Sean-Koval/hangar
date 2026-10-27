"""The control-plane image is built from uv.lock, every image it uses is pinned by digest, and it doesn't run as root
(KAN-170, security review M-5). The deployments agree with the image's user."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\b")


def _images() -> list[str]:
    """Every image the Dockerfile names: FROM lines (stage names aside), COPY --from=<image> and ARG defaults."""
    stages = set(re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", DOCKERFILE, re.M | re.I))
    args = dict(re.findall(r"^ARG\s+(\w+)=(\S+)", DOCKERFILE, re.M))
    out = []
    for ref in re.findall(r"^FROM\s+(\S+)", DOCKERFILE, re.M) + re.findall(r"--from=(\S+)", DOCKERFILE):
        ref = re.sub(r"\$\{(\w+)\}", lambda m: args.get(m.group(1), m.group(0)), ref)
        if ref not in stages:
            out.append(ref)
    return out


def test_every_image_is_pinned_by_digest():
    images = _images()
    assert len(images) >= 3, images  # rust (envd), uv and the python base
    loose = [i for i in images if not DIGEST.search(i)]
    assert not loose, f"pin these by @sha256 (keep the tag beside it): {loose}"


def test_python_packages_come_from_the_lock_with_hashes():
    assert re.search(r"COPY [^\n]*\buv\.lock\b", DOCKERFILE)
    assert "uv export --locked" in DOCKERFILE and "--require-hashes" in DOCKERFILE
    installs = re.findall(r"(?:uv )?pip install[^\n\\]*", DOCKERFILE)
    assert installs and all("--require-hashes" in i or "--no-deps" in i for i in installs), installs


def test_it_runs_as_a_non_root_user():
    users = re.findall(r"^USER\s+(\S+)", DOCKERFILE, re.M)
    assert users and users[-1].split(":")[0] not in ("0", "root"), users
    for f in ("deploy/k8s/deployment.yaml", "deploy/kustomize/base/deployment.yaml"):
        text = (ROOT / f).read_text()
        assert "runAsNonRoot: true" in text and "runAsUser: 1000" in text and "fsGroup: 1000" in text, f


def test_the_image_runs_with_a_read_only_root():
    """KAN-174 L-8: the deployments mount a read-only root, so everything the image writes at run time must be a
    volume: HANGAR_DATA_DIR (/data), /tmp and the user's home. The image must not move them elsewhere."""
    import yaml

    assert re.search(r"^ENV [^\n]*HANGAR_DATA_DIR=/data\b", DOCKERFILE, re.M)
    assert "--home-dir /home/hangar" in DOCKERFILE
    assert not re.search(r"^ENV [^\n]*\b(TMPDIR|HOME|XDG_\w+|CLOUDSDK_CONFIG|DOCKER_CONFIG)=", DOCKERFILE, re.M)
    for f in ("deploy/k8s/deployment.yaml", "deploy/kustomize/base/deployment.yaml"):
        dep = next(d for d in yaml.safe_load_all((ROOT / f).read_text()) if d and d["kind"] == "Deployment")
        (c,) = dep["spec"]["template"]["spec"]["containers"]
        assert c["securityContext"]["readOnlyRootFilesystem"] is True, f
        assert {m["mountPath"] for m in c["volumeMounts"]} >= {"/data", "/tmp", "/home/hangar"}, f
