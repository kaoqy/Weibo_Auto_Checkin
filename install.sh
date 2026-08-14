#!/usr/bin/env bash
# ============================================================
# 微博签到管理面板 · 一键部署脚本
#
# 用法：bash install.sh [端口]
#   默认端口 8000
#
# 功能：
#   1. 检测并安装 Docker / docker compose
#   2. 拉取镜像（kaoqy666/weibo-checkin:latest）或从源码构建
#   3. docker compose 启动（数据卷持久化）
#   4. 首次启动引导设置管理员账号密码（访问 init 页）
# ============================================================
set -euo pipefail

PORT="${1:-8000}"
IMAGE="kaoqy666/weibo-checkin:latest"
DATA_DIR="$(cd "$(dirname "$0")" && pwd)/data"
COMPOSE_FILE="$(cd "$(dirname "$0")" && pwd)/compose.prod.yml"

log()  { printf "\033[1;34m▶\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m✔\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m✘\033[0m %s\n" "$*" >&2; exit 1; }

# ---------- 检测 root ----------
if [ "$(id -u)" -ne 0 ]; then
  err "请用 root 运行：sudo bash install.sh"
fi

log "微博签到管理面板 · 一键部署"
log "  端口: $PORT | 数据目录: $DATA_DIR"

# ---------- 安装 Docker（若无） ----------
if ! command -v docker >/dev/null 2>&1; then
  log "未检测到 Docker，开始安装..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker 2>/dev/null || service docker start 2>/dev/null || true
  ok "Docker 已安装"
fi

# 检查 compose 插件
if ! docker compose version >/dev/null 2>&1; then
  log "安装 docker compose 插件..."
  apt-get update -y && apt-get install -y docker-compose-plugin 2>/dev/null \
    || { err "无法安装 compose 插件，请手动安装后重试"; }
  ok "compose 已安装"
fi

mkdir -p "$DATA_DIR"

# ---------- 生成 compose.prod.yml（若不存在） ----------
if [ ! -f "$COMPOSE_FILE" ]; then
  log "生成 compose.prod.yml..."
  cat > "$COMPOSE_FILE" <<YAML
name: weibo-checkin
services:
  weibo-checkin:
    image: ${IMAGE}
    container_name: weibo-checkin
    restart: unless-stopped
    ports:
      - "${PORT}:8000"
    environment:
      - TZ=Asia/Shanghai
      - APP_HOST=0.0.0.0
      - APP_PORT=8000
    volumes:
      - ${DATA_DIR}:/app/data
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "3"}
YAML
fi

# ---------- 拉取镜像 ----------
log "拉取镜像 ${IMAGE} ..."
docker pull "${IMAGE}" 2>/dev/null || err "镜像拉取失败，请检查网络或镜像名"

# ---------- 启动 ----------
log "启动容器 ..."
docker rm -f weibo-checkin 2>/dev/null || true
docker compose -f "$COMPOSE_FILE" up -d

# ---------- 健康检查 ----------
log "等待服务启动 ..."
for i in $(seq 1 15); do
  if curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

ok ""
ok "=================================================="
ok "  部署完成！"
ok "  管理面板:  http://<服务器IP>:${PORT}"
ok ""
ok "  首次访问会进入「初始化」页面，请设置管理员账号密码。"
ok "  之后用该账号登录即可使用。"
ok "=================================================="
