FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ROBOTCARE_PDF_FONT_PATH=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc

RUN apt-get update \
    && apt-get install --no-install-recommends --yes fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

# 依赖层：只依赖 pyproject.toml。改业务代码不会让这一层失效，
# 重建时直接命中缓存（否则每次改一行代码都要重装全部第三方依赖）。
# 创建临时 app 包供 setuptools 构建依赖层，安装完成后立即删除。
COPY backend/pyproject.toml backend/README.md ./
RUN mkdir -p app \
    && touch app/__init__.py \
    && pip install --no-cache-dir ".[postgres]" \
    && rm -rf app build *.egg-info

# 代码层：改动频繁，放在依赖层之后
COPY backend/alembic.ini ./
COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/docker-entrypoint.sh /usr/local/bin/robotcare-entrypoint
COPY knowledge /app/knowledge

# 依赖已在上一层安装；--no-deps 仅安装当前应用代码。
RUN pip install --no-cache-dir --no-deps ".[postgres]" \
    && addgroup --system robotcare \
    && adduser --system --ingroup robotcare --home /app robotcare \
    && mkdir -p /app/data/attachments /app/data/reports \
    && chown -R robotcare:robotcare /app \
    && chmod 0755 /usr/local/bin/robotcare-entrypoint

USER robotcare

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=12 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3).read()"]

ENTRYPOINT ["robotcare-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
