FROM python:3.13-slim

COPY requirements.txt /app/
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r /app/requirements.txt

WORKDIR /app

RUN mkdir -p /data /config

COPY . /app
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ARG VERSION=local
ARG APP_VERSION=${VERSION}
ARG COMMIT_SHA=unspecified
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_VERSION=${APP_VERSION}

LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.revision="${COMMIT_SHA}"
LABEL org.opencontainers.image.authors="AnimeWorld Companion"
LABEL org.opencontainers.image.source="https://github.com/r0bb10/AnimeWorld-Companion"

EXPOSE 7004

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7004/health')" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn awc:app --host 0.0.0.0 --port ${AWC_PORT:-7004}"]
