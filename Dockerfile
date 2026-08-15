# syntax=docker/dockerfile:1.4
# ============================================================
# 微博超话签到管理面板 · 多阶段构建
#   阶段1 build: 创建 venv 并安装依赖
#   阶段2 runtime: 精简运行镜像（仅保留 venv + 代码）
# ============================================================

# ---------- 阶段1：构建依赖 ----------
FROM python:3.11-slim AS build

WORKDIR /build

# 安装编译工具（仅构建时需要；若某些 wheel 无预编译则需 gcc）
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------- 阶段2：运行 ----------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

# 时区 + Playwright Chromium 系统依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata ca-certificates \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
        libcairo2 libatspi2.0-0 libx11-xcb1 libxshmfence1 \
        fonts-liberation \
    && ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

# 复制虚拟环境（含 playwright）
COPY --from=build --link /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 安装 Playwright 的 Chromium（无系统依赖，已在上方装好）
RUN /opt/venv/bin/playwright install chromium-headless-shell \
    && /opt/venv/bin/playwright install-deps chromium-headless-shell 2>/dev/null || true

WORKDIR /app

# 复制应用代码
COPY run.py .
COPY start.sh .
COPY app/ ./app/

# 数据目录（数据库持久化挂载点）
RUN mkdir -p /app/data

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys,os; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ['APP_PORT']+'/api/health', timeout=3).status==200 else 1)"

EXPOSE 8000

# 默认启动 Web 面板；可用 CMD 覆盖为 "checkin" 跑一次性签到
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
