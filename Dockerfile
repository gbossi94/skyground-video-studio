# Web service: API and panel.
# The worker uses Dockerfile.worker, which adds FFmpeg, Node and HyperFrames.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SKYGROUND_ENV=production \
    SKYGROUND_WORKSPACE_ROOT=/app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini studio.py ./
COPY skyground ./skyground
COPY web ./web
COPY schema ./schema

# An unprivileged user: the container writes nothing outside /tmp.
RUN useradd --create-home --uid 10001 skyground && chown -R skyground:skyground /app
USER skyground

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"10000\")}/healthz').read()"

# Render provides $PORT. One worker process per instance keeps the connection
# pool small; scale by adding instances.
CMD ["sh", "-c", "exec uvicorn skyground.asgi:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
