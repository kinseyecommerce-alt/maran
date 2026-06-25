FROM node:20-slim AS frontend-builder

WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci --prefer-offline
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt && \
    pip install --no-cache-dir --prefix=/install uvloop httptools

# ── Runtime ────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

RUN useradd -m algotrader
WORKDIR /app

COPY --from=builder /install /usr/local
COPY . .

# Overlay the compiled frontend (dist/ is gitignored; build it from source)
COPY --from=frontend-builder /frontend/dist ./frontend/dist

# Don't bake secrets into the image — mount .env at runtime
RUN rm -f .env

USER algotrader
EXPOSE 8000

# python:3.11-slim has no curl — use Python stdlib for the health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request, sys; r=urllib.request.urlopen('http://localhost:8000/health',timeout=8); sys.exit(0 if r.status==200 else 1)"

# Single worker — in-memory state (kill-switch, positions, tick engine)
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--log-level", "warning", \
     "--access-log"]
