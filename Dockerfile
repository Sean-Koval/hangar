# Control-plane image (dashboard + REST + MCP). Sandboxes are NOT built from this.
ARG BASE_IMAGE=python:3.12-slim

# --- envd: hangar-envd, the in-sandbox daemon (envd/, docs/envd.md), static for both CPU types: hangar copies it
# into sandboxes at start, and they needn't match the control plane's CPU.
FROM rust:1.97-alpine AS envd
WORKDIR /src
COPY envd ./
RUN CARGO_TARGET_DIR=/tmp/target ARCHES="x86_64 aarch64" sh build.sh

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

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system ".[all]"
COPY envs /app/envs
COPY --from=envd /src/dist/ /usr/local/lib/hangar/

ENV HANGAR_HOST=0.0.0.0 HANGAR_PORT=8787 HANGAR_DATA_DIR=/data HANGAR_ENV_DIRS='["/app/envs"]'
VOLUME /data
EXPOSE 8787
HEALTHCHECK CMD curl -fsS http://localhost:8787/healthz || exit 1
CMD ["hangar", "serve"]
