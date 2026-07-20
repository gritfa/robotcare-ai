FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ROBOTCARE_PDF_FONT_PATH=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc

RUN apt-get update \
    && apt-get install --no-install-recommends --yes fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

COPY backend/pyproject.toml backend/README.md backend/alembic.ini ./
COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/docker-entrypoint.sh /usr/local/bin/robotcare-entrypoint
COPY knowledge /app/knowledge

RUN pip install --no-cache-dir ".[postgres]" \
    && addgroup --system robotcare \
    && adduser --system --ingroup robotcare --home /app robotcare \
    && mkdir -p /app/data/attachments /app/data/reports \
    && chown -R robotcare:robotcare /app \
    && chmod 0755 /usr/local/bin/robotcare-entrypoint

USER robotcare

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=12 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

ENTRYPOINT ["robotcare-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
