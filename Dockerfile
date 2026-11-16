# Barcode Document Sorter
FROM python:3.12-slim-bookworm

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    HOME=/tmp \
    TMPDIR=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
       --home-dir /tmp --shell /usr/sbin/nologin app

COPY --chown=app:app app ./app

RUN mkdir -p /app/input /app/output /app/data /app/logs \
    && chown -R app:app /app/output /app/data /app/logs \
    && chmod 0750 /app/output /app/logs \
    && chmod 0700 /app/data

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=6s --start-period=40s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]

CMD ["python", "-m", "app.main"]
