FROM node:22-alpine AS frontend
WORKDIR /src/web/frontend
COPY web/frontend/package*.json ./
RUN npm ci
COPY web/frontend ./
RUN npm run build

FROM python:3.12-slim AS backend
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SSL_SYNC_DATA_DIR=/var/lib/ssl-cert-sync \
    SSL_SYNC_LOG_DIR=/var/log/ssl-cert-sync \
    SSL_SYNC_FRONTEND_DIST=/app/frontend-dist

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash curl git openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY web/backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN git clone --depth=1 https://github.com/acmesh-official/acme.sh.git /opt/acme.sh \
    && rm -rf /opt/acme.sh/.git \
    && chmod 755 /opt/acme.sh/acme.sh

COPY web/backend/app /app/app
COPY cert-master-sync.sh /usr/local/bin/cert-master-sync.sh
RUN chmod 750 /usr/local/bin/cert-master-sync.sh
COPY cert-node-agent.sh cert-node-pull.sh cert-puller.service cert-puller.timer /opt/ssl-sync-node/
RUN chmod 750 /opt/ssl-sync-node/cert-node-agent.sh /opt/ssl-sync-node/cert-node-pull.sh
COPY --from=frontend /src/web/frontend/dist /app/frontend-dist

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info", "--access-log"]
