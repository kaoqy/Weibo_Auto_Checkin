#!/usr/bin/env bash
# ============================================================
# 微博签到管理面板 · 一键安装/更新脚本
#
# 用法：
#   bash install.sh                # 安装/启动（默认端口 8000）
#   bash install.sh 8080           # 安装/启动，指定端口
#   bash install.sh update         # 拉取最新镜像并更新容器（保留数据）
#   bash install.sh start          # 启动容器
#   bash install.sh stop           # 停止容器
#   bash install.sh restart        # 重启容器
#   bash install.sh status         # 查看容器状态
#   bash install.sh logs           # 查看日志
#
# 环境变量：
#   WCM_IMAGE     镜像名（默认 kaoqy666/weibo-checkin:latest）
#   WCM_PORT      端口（默认 8000，可用作参数代替）
#   WCM_DATA      数据目录（默认 <脚本目录>/data）
#
# 功能：
#   1. 检测并安装 Docker / docker compose
#   2. 拉取镜像（kaoqy666/weibo-checkin:latest）
#   3. docker compose 启动（数据卷持久化）
#   4. 首次启动引导设置管理员账号密码（访问 init 页）
#   5. update：拉最新镜像 + 重建容器，数据不丢
# ============================================================
set -euo pipefail

# ---------- 解析参数 ----------
CMD="${1:-install}"
if [[ "$CMD" =~ ^[0-9]+$ ]]; then
  # 兼容旧用法：bash install.sh 8000
  PORT="$CMD"
  CMD="install"
else
  PORT="${WCM_PORT:-8000}"
fi
COMMAND="$CMD"

IMAGE="${WCM_IMAGE:-kaoqy666/weibo-checkin:latest}"
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${WCM_DATA:-$BASE_DIR/data}"
COMPOSE_FILE="$BASE_DIR/compose.prod.yml"

log()  { printf "\033[1;34m▶\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m✔\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m✘\033[0m %s\n" "$*" >&2; exit 1; }

# ---------- 检测 root ----------
if [ "$(id -u)" -ne 0 ] && [ "$COMMAND" != "status" ] && [ "$COMMAND" != "logs" ]; then
  err "请用 root 运行：sudo bash install.sh"
fi

# ---------- 确保 compose 文件 ----------
ensure_compose() {
  mkdir -p "$DATA_DIR"
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
    ok "compose 配置已生成"
  fi
}

# ---------- 安装 Docker（若无） ----------
ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    log "未检测到 Docker，开始安装..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker 2>/dev/null || service docker start 2>/dev/null || true
    ok "Docker 已安装"
  fi
  if ! docker compose version >/dev/null 2>&1; then
    log "安装 docker compose 插件..."
    apt-get update -y && apt-get install -y docker-compose-plugin 2>/dev/null \
      || { err "无法安装 compose 插件，请手动安装后重试"; }
    ok "compose 已安装"
  fi
}

# ---------- 健康等待 ----------
wait_health() {
  log "等待服务启动..."
  for i in $(seq 1 20); do
    if curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
      ok "服务已就绪（端口 $PORT）"
      return 0
    fi
    sleep 1
  done
  err "服务启动超时，请查看日志：bash install.sh logs"
}

# ---------- 安装/启动 ----------
do_install() {
  log "微博签到管理面板 · 一键部署"
  log "  端口: $PORT | 数据目录: $DATA_DIR | 镜像: $IMAGE"
  ensure_docker
  ensure_compose

  log "拉取镜像 ${IMAGE} ..."
  docker pull "${IMAGE}" 2>/dev/null || err "镜像拉取失败，请检查网络或镜像名"

  log "启动容器 ..."
  docker rm -f weibo-checkin 2>/dev/null || true
  docker compose -f "$COMPOSE_FILE" up -d
  wait_health

  ok ""
  ok "=================================================="
  ok "  部署完成！"
  ok "  管理面板:  http://<服务器IP>:${PORT}"
  ok ""
  ok "  首次访问会进入「初始化」页面，请设置管理员账号密码。"
  ok "  之后用该账号登录即可使用。"
  ok "  更新: bash install.sh update"
  ok "=================================================="
}

# ---------- 更新 ----------
do_update() {
  log "更新微博签到面板 → $IMAGE"
  if ! command -v docker >/dev/null 2>&1; then
    err "未安装 Docker，请先运行 bash install.sh"
  fi
  ensure_compose

  log "拉取最新镜像 ..."
  docker pull "${IMAGE}" || err "镜像拉取失败"

  log "重建容器（数据在 $DATA_DIR，不会丢失）..."
  docker compose -f "$COMPOSE_FILE" up -d --remove-orphans
  wait_health

  NEW_VER="$(docker exec weibo-checkin sh -c 'echo ok' >/dev/null 2>&1 && \
    curl -sf "http://127.0.0.1:${PORT}/api/health" 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
  ok "更新完成！当前版本: v${NEW_VER:-?}"
  ok "管理面板: http://<服务器IP>:${PORT}"
}

# ---------- 状态/日志 ----------
do_status() {
  docker ps --filter name=weibo-checkin --format '状态: {{.Status}} | 镜像: {{.Image}} | 端口: {{.Ports}}'
  echo "镜像版本: $(docker exec weibo-checkin cat /app/app/main.py 2>/dev/null | sed -n 's/.*version="\([^"]*\)".*/\1/p' || echo '容器未运行')"
}

# ---------- 主流程 ----------
case "$COMMAND" in
  install)      do_install ;;
  update)       do_update ;;
  start)        ensure_compose; docker compose -f "$COMPOSE_FILE" up -d; ok "已启动" ;;
  stop)         ensure_compose; docker compose -f "$COMPOSE_FILE" stop; ok "已停止" ;;
  restart)      ensure_compose; docker compose -f "$COMPOSE_FILE" restart; wait_health ;;
  status)       do_status ;;
  logs)         docker logs --tail 50 -f weibo-checkin ;;
  *)
    echo "用法: bash install.sh [install|update|start|stop|restart|status|logs|<端口>]"
    exit 1
    ;;
esac

ok "全部完成"
