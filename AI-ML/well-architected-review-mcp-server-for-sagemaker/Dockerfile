# syntax=docker/dockerfile:1

# Amazon Bedrock AgentCore Runtime requires an ARM64 container listening on
# 0.0.0.0:8000 with a POST /mcp endpoint, using stateless streamable-HTTP.
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp-protocol-contract.html
#
#   docker build -t wa-sagemaker-mcp:al2023 .

# ============================================================================
# Stage 1 — builder (throwaway): installs uv + a standalone CPython 3.12 and
# bakes the self-contained virtualenv. None of this stage ships.
# ============================================================================
FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023@sha256:12052e9b5d3fd85769abbdd863dd038e1890c9ace31d5fdbe1afa78eda97d061 AS builder

ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_LINK_MODE=copy

RUN dnf -y install python3-pip \
 && dnf clean all \
 && rm -rf /var/cache/dnf

RUN pip3 install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY well_architected_review_mcp_server_for_sagemaker ./well_architected_review_mcp_server_for_sagemaker
COPY main.py README.md ./

# --frozen: fail rather than silently re-resolving if uv.lock is stale.
# --no-dev: omit ruff/pyright/pytest. --python 3.12: pin interpreter.
# Drop the uv download cache AND the interpreter's bundled installer tooling
# (ensurepip + pip, whose vendored urllib3/msgpack/setuptools are the only
# thing a scan would otherwise flag) so only the venv + interpreter carry
# forward. The uv-created venv is pip-less and never uses them.
RUN uv python install 3.12 \
 && uv sync --frozen --no-dev --python 3.12 \
 && uv cache clean \
 && rm -rf /root/.cache/uv \
 && rm -rf /opt/uv/python/*/lib/python3.12/ensurepip \
           /opt/uv/python/*/lib/python3.12/site-packages/pip \
           /opt/uv/python/*/lib/python3.12/site-packages/pip-*.dist-info \
 && chmod -R a+rX /opt/uv

# ============================================================================
# Stage 2 — runtime: clean, Amazon-patched AL2023 with NO uv, pip, or build
# tooling. ECR-native base closes PCSR Finding 1 by provenance, and is pinned
# by digest for reproducibility. Re-resolve the digest periodically to pick up
# Amazon security patches:
#   docker pull public.ecr.aws/amazonlinux/amazonlinux:2023
#   docker inspect --format '{{index .RepoDigests 0}}' public.ecr.aws/amazonlinux/amazonlinux:2023
# ============================================================================
FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023@sha256:12052e9b5d3fd85769abbdd863dd038e1890c9ace31d5fdbe1afa78eda97d061

# Amazon-published OS security patches only.
RUN dnf -y upgrade \
 && dnf clean all \
 && rm -rf /var/cache/dnf

# Non-root identity without shadow-utils (keeps the image minimal). A numeric
# UID runs with no passwd entry, but we add one so getpwuid() resolves cleanly.
RUN printf 'app:x:10001:10001::/app:/sbin/nologin\n' >> /etc/passwd \
 && printf 'app:x:10001:\n' >> /etc/group

# Carry over ONLY the standalone interpreter and the baked venv + app code.
# /opt/uv/python is world-readable (chmod above); the venv is owned by the
# non-root user. No network, pip, or uv at or after container start.
COPY --from=builder /opt/uv/python /opt/uv/python
COPY --from=builder --chown=10001:10001 /app /app

WORKDIR /app
USER 10001:10001

EXPOSE 8000

ENTRYPOINT ["/app/.venv/bin/well-architected-review-mcp-server-for-sagemaker", \
            "--transport", "streamable-http", \
            "--allow-sensitive-data-access"]
