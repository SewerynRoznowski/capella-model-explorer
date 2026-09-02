# Copyright DB InfraGO AG and contributors
# SPDX-License-Identifier: Apache-2.0

# ============================================================
# Base image
# ============================================================

FROM python:3.12-slim-bookworm AS base

USER root

WORKDIR /app

ENV HOME=/home
ENV PATH=/home/.local/bin:/app/bin:/usr/local/bin:$PATH
ENV VIRTUAL_ENV=/app
ENV MODEL_ENTRYPOINT=/model
ENV CME_LIVE_MODE=0

EXPOSE 8000

# ============================================================
# System dependencies
# ============================================================

RUN apt-get update && \
    apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        git \
        git-lfs \
        gnupg \
        graphviz \
        libcairo2-dev \
        libgirepository1.0-dev \
        gir1.2-pango-1.0 \
    && \
    rm -rf /var/lib/apt/lists/*

# ============================================================
# Node.js 22
# ============================================================

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get update && \
    apt-get install --yes --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# ============================================================
# Build stage
# ============================================================

FROM base AS build

USER root

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# ------------------------------------------------------------
# Install uv
# ------------------------------------------------------------

RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -sf /home/.local/bin/uv /usr/local/bin/uv

# ------------------------------------------------------------
# Install pnpm
# ------------------------------------------------------------

RUN npm install --global pnpm@11

# ------------------------------------------------------------
# Verify toolchain
# ------------------------------------------------------------

RUN echo "=== Node ===" && \
    node --version && \
    echo "=== npm ===" && \
    npm --version && \
    echo "=== pnpm ===" && \
    pnpm --version && \
    echo "=== uv ===" && \
    uv --version

# ------------------------------------------------------------
# Copy CME source
# ------------------------------------------------------------

COPY . /build

WORKDIR /build

# ------------------------------------------------------------
# Allow required native dependency build scripts
#
# pnpm 11 uses allowBuilds.
# ------------------------------------------------------------

RUN printf '%s\n' \
    'allowBuilds:' \
    '  "@parcel/watcher": true' \
    '  "@swc/core": true' \
    '  "lmdb": true' \
    '  "msgpackr-extract": true' \
    > pnpm-workspace.yaml

RUN cat pnpm-workspace.yaml

# ------------------------------------------------------------
# Build CME frontend
# ------------------------------------------------------------

RUN uv run cme build

# ------------------------------------------------------------
# Create the actual runtime environment
#
# IMPORTANT:
# Do this from /build, where pyproject.toml lives.
# ------------------------------------------------------------

RUN uv venv /app

RUN uv sync \
    --active \
    --locked \
    --no-default-groups \
    --no-editable

# ------------------------------------------------------------
# Override capellambse-context-diagrams with a custom fork/branch
# ------------------------------------------------------------

RUN uv pip install \
    --python /app/bin/python \
    --no-deps \
    --force-reinstall \
    "capellambse-context-diagrams @ git+https://github.com/SewerynRoznowski/capellambse-context-diagrams.git@Add-Requirements-context-diagram"

# ------------------------------------------------------------
# Verify that the runtime environment really contains CME
# ------------------------------------------------------------

RUN /app/bin/python -c "import capellambse; print('capellambse OK')" && \
    /app/bin/python -c "import capellambse_context_diagrams; print('context diagrams OK')" && \
    /app/bin/python -c "import capella_model_explorer; print('CME OK')"

# ============================================================
# Runtime image
# ============================================================

FROM base

USER root

# ------------------------------------------------------------
# Runtime directories
# ------------------------------------------------------------

RUN mkdir -p /model /data

# ------------------------------------------------------------
# Runtime entrypoint
# ------------------------------------------------------------

COPY --chown=0:0 --chmod=755 entrypoint.sh /entrypoint.sh

# ------------------------------------------------------------
# Copy Python environment from build stage
# ------------------------------------------------------------

COPY --from=build --chown=0:0 /app /app

# ------------------------------------------------------------
# Copy generated CME files
# ------------------------------------------------------------

COPY --from=build /build/templates /templates
COPY --from=build /build/static /data/static

# ------------------------------------------------------------
# Install ELK
# ------------------------------------------------------------

RUN /app/bin/python -c \
    "from capellambse_context_diagrams import install_elk; install_elk()"

# ------------------------------------------------------------
# Git permissions for mounted Capella models
# ------------------------------------------------------------

RUN git config --global --add safe.directory /model && \
    git config --global --add safe.directory /model/.git && \
    chmod -R a=rwX /home

WORKDIR /data

USER 1000

ENTRYPOINT ["/entrypoint.sh"]
