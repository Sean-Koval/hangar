# Control-plane image (dashboard + REST + MCP). Sandboxes are NOT built from this.
# Every image is pinned by digest and Python packages come from uv.lock with hashes (KAN-170, security review M-5).
# Bump a digest by re-resolving its tag; keep the tag beside it so the bump reads in a diff.
ARG BASE_IMAGE=python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# --- envd: hangar-envd, the in-sandbox daemon (envd/, docs/envd.md), static for both CPU types: hangar copies it
# into sandboxes at start, and they needn't match the control plane's CPU.
FROM rust:1.97-alpine@sha256:3c38f3f82c2f3d73da3b38e18d279393a04cb43ddded0e35088a8c3324d40900 AS envd
WORKDIR /src
COPY envd ./
RUN CARGO_TARGET_DIR=/tmp/target ARCHES="x86_64 aarch64" sh build.sh

FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM ${BASE_IMAGE}

ARG INSTALL_GCLOUD=1
ARG INSTALL_DOCKER_CLI=1
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 UV_SYSTEM_PYTHON=1

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg git \
 && if [ "$INSTALL_DOCKER_CLI" = 1 ]; then \
      install -m 0755 -d /etc/apt/keyrings && \
      curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
      echo "deb [signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list && \
      apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin; fi \
 && if [ "$INSTALL_GCLOUD" = 1 ]; then \
      curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
      echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list && \
      apt-get update && apt-get install -y --no-install-recommends google-cloud-cli; fi \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# The dependencies exactly as uv.lock pins them, each checked against its hash (--locked fails on a stale lock);
# then hangar itself, without resolving anything again.
RUN uv export --locked --no-dev --extra all --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
 && uv pip install --system --require-hashes -r /tmp/requirements.txt \
 && uv pip install --system --no-deps . \
 && rm /tmp/requirements.txt
COPY envs /app/envs
COPY --from=envd /src/dist/ /usr/local/lib/hangar/

# Not root: uid/gid 1000, the pod's fsGroup (deploy/k8s/deployment.yaml). The compose file runs it as root, since
# its Docker provider needs the host's socket, which is root-equivalent anyway.
RUN groupadd --gid 1000 hangar && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/hangar hangar \
 && install -d -o 1000 -g 1000 -m 0700 /data
ENV HANGAR_HOST=0.0.0.0 HANGAR_PORT=8787 HANGAR_DATA_DIR=/data HANGAR_ENV_DIRS='["/app/envs"]'
VOLUME /data
USER 1000:1000
EXPOSE 8787
HEALTHCHECK CMD curl -fsS http://localhost:8787/healthz || exit 1
CMD ["hangar", "serve"]
