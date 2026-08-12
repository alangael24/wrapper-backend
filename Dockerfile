# syntax=docker/dockerfile:1.7
FROM node:22.19.0-bookworm-slim AS node-runtime

FROM python:3.12.11-slim-bookworm

ARG PNPM_VERSION=11.16.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PNPM_HOME=/opt/pnpm \
    PATH=/opt/venv/bin:/opt/pnpm:/usr/local/bin:/usr/bin:/bin \
    HOST=0.0.0.0 \
    PORT=8787 \
    ENVIRONMENT=production \
    PI_BIN=/app/scripts/pi-sandbox \
    PI_CHROME_BIN=/usr/bin/chromium

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN set -eux; \
    ln -s /usr/local/lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack; \
    corepack enable; \
    corepack prepare "pnpm@${PNPM_VERSION}" --activate; \
    apt-get update; \
    apt-get install -y --no-install-recommends bubblewrap ca-certificates chromium curl socat tini util-linux; \
    rm -rf /var/lib/apt/lists/*; \
    python -m venv "$VIRTUAL_ENV"

WORKDIR /app
COPY requirements.txt package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pnpm install --prod --frozen-lockfile

COPY go_backend ./go_backend
COPY extensions ./extensions
COPY scripts ./scripts
COPY run.sh README.md ./

RUN set -eux; \
    chmod 0755 run.sh scripts/pi-sandbox scripts/setup-pi-sandbox.sh; \
    mkdir -p /app/data/pi-runs; \
    chown -R 10001:10001 /app/data

USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8787/healthz >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "go_backend.server", "serve", "--port", "8787"]
