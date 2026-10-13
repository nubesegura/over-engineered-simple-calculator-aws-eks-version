# One image for every workload of the calculator: `calculator <add|sub|mul|div|history|migrate>`.
#
# Build (BuildKit), from the repository root:
#   docker build --build-context rotation=src/rotation -f src/backend/Dockerfile \
#     -t <repository>:<tag> src/backend
# The named context `rotation` lets the CA bundle stage reuse the validated download logic of
# src/rotation/package_bundle.py instead of duplicating it.
#
# Base image: python:3.14-slim-trixie, multi-arch index pinned by digest (the digest the ecs
# image resolved on 2026-10-05, built for linux/arm64 because the nodes are Graviton).
# Refresh tag and digest together, never use `latest`.
ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151

# ---- build stage: runtime dependencies and the calculator_core package ----
# hadolint ignore=DL3029
FROM --platform=linux/arm64 ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /build
# Runtime pins only (requirements.txt, every version with ==); the dev tools are never installed.
COPY calculator_core/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
COPY calculator_core/pyproject.toml ./calculator_core/pyproject.toml
COPY calculator_core/src ./calculator_core/src
RUN pip install --no-deps ./calculator_core

# ---- CA bundle stage: one trust file for libpq, built and validated with the rotation helper ----
# The file holds the RDS global bundle (direct connections to the instance: the migration job) plus the four
# Amazon Trust Services roots (RDS Proxy presents an ACM certificate that chains to them, not to the RDS CAs).
# Everything is downloaded over verified HTTPS; the Amazon roots are pinned by SHA-256 in package_bundle.py.
# hadolint ignore=DL3029
FROM --platform=linux/arm64 ${PYTHON_IMAGE} AS bundle
COPY --from=rotation package_bundle.py /helper/package_bundle.py
RUN python /helper/package_bundle.py --trust-bundle /db-trust-bundle.pem

# ---- runtime stage ----
# hadolint ignore=DL3029
FROM --platform=linux/arm64 ${PYTHON_IMAGE} AS runtime

# Non-root user without a login shell or home directory.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --from=bundle --chown=root:root --chmod=0444 /db-trust-bundle.pem /app/db-trust-bundle.pem
COPY --chown=root:root migrations /app/migrations

# Code, CA bundle and migration files are read-only for the process. The only writable path is
# /tmp, a volume the chart mounts (the self-signed certificate and key are written there).
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MIGRATIONS_DIRECTORY=/app/migrations

USER 10001:10001
EXPOSE 8443
# No HEALTHCHECK: the probes are defined in the chart (GET /health over HTTPS).
ENTRYPOINT ["calculator"]
