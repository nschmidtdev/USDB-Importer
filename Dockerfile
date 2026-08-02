FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="UltraStar Importer" \
      org.opencontainers.image.description="UltraStar karaoke library manager - authenticated server mode" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.licenses="GPL-3.0-only"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-server.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements-server.txt

RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/output /app/data \
    && chown -R app:app /app

COPY --chown=app:app src/ ./src/
COPY --chown=app:app server.py ./server.py
COPY --chown=app:app static/ ./static/
COPY --chown=app:app config.example.json ./
COPY --chown=app:app LICENSE DISCLAIMER.md THIRD_PARTY_NOTICES.md THIRD_PARTY_LICENSES.txt ./

ENV USDB_HOST=0.0.0.0 \
    USDB_PORT=5776 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER app

EXPOSE 5776
VOLUME ["/app/output", "/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5776/health', timeout=3)" || exit 1

CMD ["python", "server.py"]
