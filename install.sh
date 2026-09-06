#!/usr/bin/env bash
# ============================================================
# 微博签到管理面板 · 一键安装/更新脚本 (v1.2.0+)
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
#   bash install.sh mirror         # 仅重写 Docker 国内镜像源配置（已安装 Docker 时也可单独跑）
#
# 环境变量：
#   WCM_IMAGE         镜像名（默认 kaoqy666/weibo-checkin:latest）
#   WCM_PORT          端口（默认 8000，可用作参数代替）
#   WCM_DATA          数据目录（默认 <脚本目录>/data）
#   WCM_SKIP_MIRROR=1 跳过自动写入 Docker 国内镜像源（默认会自动检测）
#   WCM_FORCE_MIRROR=1 不论地域都写入国内镜像源（绕过网络限制时使用）
#
# 功能：
#   1. 检测并安装 Docker / docker compose
#   2. **自动识别服务器所在国家，若在国内则写入 Docker 国内 registry-mirrors**
#      （避免 docker.io 在国内被墙/拉镜像极慢；仅写入 /etc/docker/daemon.json，不影响其他容器）
#   3. 拉取镜像（kaoqy666/weibo-checkin:latest）
#   4. docker compose 启动（数据卷持久化）
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
warn() { printf "\033[1;33m⚠\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m✘\033[0m %s\n" "$*" >&2; exit 1; }

# ---------- root 检查 ----------
if [ "$(id -u)" -ne 0 ] && [ "$COMMAND" != "status" ] && [ "$COMMAND" != "logs" ] && [ "$COMMAND" != "mirror" ]; then
  err "请用 root 运行：sudo bash install.sh"
fi

# ============================================================
#  Docker 国内镜像源自动配置
# ============================================================

# 多个候选公网 IP 检测服务（并行请求，超时短，能跑通即用）。
# 返回机器出口公网 IP（stdout），失败返回空字符串。
_detect_public_ip() {
  local out=""
  local services=(
    "https://api.ipify.org"
    "https://ifconfig.me/ip"
    "https://checkip.amazonaws.com"
    "https://ip.sb"
  )
  for url in "${services[@]}"; do
    out="$(curl -fsS --max-time 6 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$out" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo "$out"
      return 0
    fi
  done
  echo ""
  return 1
}

# 用 ip-api.com 免费接口查归属地（仅需 IP，不需要 API key；返回 JSON）。
# 只取 countryCode 字段，避免依赖 JSON 解析器；返回 CN / US / HK 等大写国家码。
_detect_country() {
  local ip="$1"
  local body=""
  body="$(curl -fsS --max-time 8 "http://ip-api.com/json/${ip}?fields=status,countryCode,query" 2>/dev/null || true)"
  if [ -z "$body" ]; then
    echo ""
    return 1
  fi
  # 极简解析：找 "countryCode":"XX"
  local cc
  cc="$(printf '%s' "$body" | grep -oE '"countryCode"[[:space:]]*:[[:space:]]*"[A-Z]{2}"' | head -1 | grep -oE '[A-Z]{2}' | tail -1)"
  echo "${cc:-}"
}

# 一组国内常用的 registry mirror。优先顺序按实测连通性/速度排。
# dockerproxy.com / docker.m.daocloud.io 都支持 https，可直接写 daemon.json。
_CN_MIRRORS=(
  "https://docker.1ms.run"
  "https://docker.m.daocloud.io"
  "https://docker.ketches.cn"
  "https://dockerproxy.cn"
  "https://hub-mirror.c.163.com"
  "https://mirror.baidubce.com"
)

# 把 mirror 列表写入 /etc/docker/daemon.json；保留用户已有的设置。
# 仅当用户显式说 WCM_SKIP_MIRROR=1 才跳过。
configure_docker_mirrors() {
  if [ "${WCM_SKIP_MIRROR:-0}" = "1" ]; then
    warn "WCM_SKIP_MIRROR=1，跳过 Docker 国内镜像源配置"
    return 0
  fi

  # 已是国内 → 直接写
  if [ "${WCM_FORCE_MIRROR:-0}" = "1" ]; then
    warn "WCM_FORCE_MIRROR=1：强制写入国内镜像源"
    _write_mirrors
    return 0
  fi

  log "检测服务器所在国家（判断是否需要 Docker 国内镜像源）..."
  local ip country=""
  ip="$(_detect_public_ip)"
  if [ -z "$ip" ]; then
    warn "无法获取公网 IP（可能无外网或被防火墙拦），跳过镜像源自动配置"
    warn "若拉镜像慢，可手动：bash install.sh mirror"
    return 0
  fi
  country="$(_detect_country "$ip")"
  log "  出口 IP: $ip  归属地: ${country:-?}"

  case "$country" in
    CN|HK|MO)
      log "检测到国内/港澳出口，配置 Docker registry-mirrors..."
      _write_mirrors
      ;;
    "")
      warn "归属地识别失败，跳过（可手动：bash install.sh mirror）"
      ;;
    *)
      log "归属地 $country，跳过 Docker 镜像源配置"
      ;;
  esac
}

_write_mirrors() {
  mkdir -p /etc/docker
  local daemon_json="/etc/docker/daemon.json"

  # 拼装新 mirror JSON 数组（保留顺序）
  local mlist=""
  for m in "${_CN_MIRRORS[@]}"; do
    if [ -z "$mlist" ]; then mlist="\"$m\""; else mlist="$mlist, \"$m\""; fi
  done

  # 读已有 daemon.json，浅合并 registry-mirrors 项。
  local existing="{}"
  if [ -f "$daemon_json" ]; then
    if command -v python3 >/dev/null 2>&1; then
      existing="$(python3 -c 'import json,sys;
p="/etc/docker/daemon.json"
try:
    d=json.load(open(p))
except Exception:
    d={}
d.setdefault("registry-mirrors", [])
print(json.dumps(d, ensure_ascii=False))
' 2>/dev/null || echo '{}')"
    fi
  fi

  if command -v python3 >/dev/null 2>&1; then
    python3 - <<PY
import json
p = "/etc/docker/daemon.json"
try:
    with open(p, "r", encoding="utf-8") as f:
        cfg = json.load(f)
except Exception:
    cfg = {}
cfg["registry-mirrors"] = ${mlist}
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print("ok")
PY
    ok "已写入 /etc/docker/daemon.json（registry-mirrors）"
  else
    # 没 python3 就用 sed 简单 append（不解析 JSON，仅首次初始化用）
    if [ ! -f "$daemon_json" ]; then
      cat > "$daemon_json" <<JSON
{
  "registry-mirrors": [${mlist}]
}
JSON
      ok "已生成 /etc/docker/daemon.json"
    else
      warn "未检测到 python3，跳过 mirror 配置（保留原 daemon.json）"
      return 0
    fi
  fi

  # 触发 reload（如果 docker 已启动）
  if command -v systemctl >/dev/null 2>&1; then
    systemctl reload docker 2>/dev/null && ok "dockerd 已 reload" \
      || warn "dockerd reload 失败（首次安装时正常）"
  fi
}

do_mirror() {
  # 单独运行镜像源配置：无需 docker 安装
  if [ "${WCM_FORCE_MIRROR:-0}" = "1" ] || [ "${WCM_SKIP_MIRROR:-0}" = "1" ]; then
    _write_mirrors
    return 0
  fi
  configure_docker_mirrors
}

# ============================================================
#  容器编排
# ============================================================

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

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    log "未检测到 Docker，开始安装..."
    # 先尝试国内镜像安装 get-docker.sh（可选）
    local inst_url="https://get.docker.com"
    if [ "${WCM_FORCE_MIRROR:-0}" = "1" ]; then
      local alt_url="https://get.daocloud.io/docker-ce"
      if curl -fsSI --max-time 6 "$alt_url" >/dev/null 2>&1; then
        inst_url="$alt_url"
      fi
    fi
    curl -fsSL "$inst_url" | sh
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

# 健康等待
wait_health() {
  log "等待服务启动..."
  for i in $(seq 1 30); do
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
  configure_docker_mirrors     # 仅在 install/update 时尝试（幂等）
  ensure_compose

  log "拉取镜像 ${IMAGE} ..."
  docker pull "${IMAGE}" 2>/dev/null || err "镜像拉取失败，请检查网络或镜像名（可手动：bash install.sh mirror）"

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

# ---------- 更新（幂等：能跑就跑，跑不动也能干净恢复）----------
do_update() {
  log "更新微博签到面板 → $IMAGE"
  if ! command -v docker >/dev/null 2>&1; then
    err "未安装 Docker，请先运行 bash install.sh"
  fi
  configure_docker_mirrors
  ensure_compose

  log "拉取最新镜像 ..."
  docker pull "${IMAGE}" || err "镜像拉取失败（可手动：bash install.sh mirror）"

  log "停止并删除旧容器 ..."
  docker stop weibo-checkin 2>/dev/null || true
  docker rm weibo-checkin 2>/dev/null || true

  log "启动新容器（数据在 $DATA_DIR，不会丢失）..."
  docker compose -f "$COMPOSE_FILE" up -d
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
  mirror)       do_mirror ;;
  *)
    echo "用法: bash install.sh [install|update|start|stop|restart|status|logs|mirror|<端口>]"
    exit 1
    ;;
esac

ok "全部完成"