FROM python:3.11-slim AS base

# ==========================================
# Mirrors for Chinese users (build args)
#   APT_MIRROR_HOST: apt mirror hostname
#   PIP_INDEX_URL:   PyPI mirror URL
# ==========================================
ARG APT_MIRROR_HOST=deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Switch apt mirror (for Chinese users)
RUN sed -i "s|URIs: http://deb.debian.org|URIs: http://${APT_MIRROR_HOST}|g" /etc/apt/sources.list.d/debian.sources

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

FROM base AS runner

WORKDIR /app

# 先拷贝依赖清单以利用 Docker 缓存
COPY apps/api/pyproject.toml /app/
COPY packages/ /app/packages/

# 安装 workspace 依赖（tablo）和所有 Python 依赖
RUN python3 -c "import tomllib, os; deps = tomllib.load(open('/app/pyproject.toml','rb'))['project']['dependencies']; open('/tmp/requirements.txt','w').write('\n'.join(d for d in deps if not d.startswith('tablo')))" && \
    pip install /app/packages/tablo && \
    pip install -r /tmp/requirements.txt && \
    rm -f /tmp/requirements.txt

# 拷贝 package.json（用于生成版本）
COPY package.json /tmp/

# 拷贝应用代码
COPY apps/api/ /app/

# 生成版本文件
RUN python scripts/generate_version.py

# FastAPI 默认端口
EXPOSE 8000

# 默认命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
