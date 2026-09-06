#!/usr/bin/env bash
# ============================================================
# 微博签到管理面板 · 一键安装/更新脚本 (v1.2.1+)
#
# 这是终端用户唯一需要的脚本。镜像构建与推送已全部交给
# GitHub Actions（push tag v*.*.* 即自动构建并发布）。
#
# 用法：
#   bash install.sh                # 安装/启动（默认端口 8000）
#   bash install.sh 8080           # 安装/启动，指定端口
#   bash install.sh update         # 拉最新镜像并更新容器（保留数据）
#   bash install.sh start|stop|restart|status|logs
#   bash install.sh mirror         # 仅重写 Docker 国内镜像源配置（已安装 Docker 时也可单独跑）
#
# 环境变量：
#   WCM_IMAGE         镜像名（默认 kaoqy666/weibo-checkin:latest）
#   WCM_PORT          端口（默认 8000，可用作参数代替）
#   WCM_DATA          数据目录（默认 <脚本目录>/data）
#   WCM_SKIP_MIRROR=1   跳过自动写入 Docker 国内镜像源
#   WCM_FORCE_MIRROR=1  不论地域都写入国内镜像源（绕过网络限制）
#   WCM_NO_MIRROR_FALLBACK=1  关闭「镜像拉不动时回退国内 mirror」兜底
#
# 自动行为：
#   1. 检测并安装 Docker / docker compose
#   2. **检测服务器所在国家**：若 CN/HK/MO 则自动写入 Docker 国内
#      registry-mirrors（实测连通性，选用能连通的几个），并触发 dockerd reload
#   3. `docker pull` 失败时，**自动回退到国内 mirror 源**（用 `docker pull
#      <mirror>/kaoqy666/weibo-checkin:tag`，再 re-tag 成原名）—— 不再因为
#      跨境网络抖动就装不上
#   4. compose 启动（数据卷持久化）、健康检查
# ============================================================
set -euo pipefail

# ---------- 解析参数 ----------
CMD="${1:-install}"
if [[ "$CMD" =~ ^[0-9]+$ ]]; then
  PORT="$CMD"; CMD="install"
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
NEED_ROOT="install|update|start|stop|restart|mirror"
if [ "$(id -u)" -ne 0 ] && [[ "|$NEED_ROOT|" == *"|$COMMAND|"* ]]; then
  err "请用 root 运行：sudo bash install.sh"
fi

# ============================================================
#  CN 检测 + Docker 国内镜像源
# ============================================================

# 国内常用 mirror（按实测连通性/速度排序）
_CN_MIRRORS=(
  "https://docker.1ms.run"
  "https://docker.m.daocloud.io"
  "https://docker.ketches.cn"
  "https://dockerproxy.cn"
  "https://hub-mirror.c.163.com"
  "https://mirror.baidubce.com"
)

# 取机器的公网 IP（多个候选并行，stdout 为 IP；失败返回空）
_detect_public_ip() {
  local ip=""
  local services=(
    "https://api.ipify.org"
    "https://ifconfig.me/ip"
    "https://checkip.amazonaws.com"
    "https://ip.sb"
    "https://ipv4.icanhazip.com"
  )
  for url in "${services[@]}"; do
    ip="$(curl -fsS --max-time 6 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && { echo "$ip"; return 0; }
  done
  return 1
}

# 用 ip-api.com 免费接口查归属地（返回大写两字母国家码）
_detect_country() {
  local ip="$1"
  local body=""
  body="$(curl -fsS --max-time 8 "http://ip-api.com/json/${ip}?fields=status,countryCode" 2>/dev/null || true)"
  printf '%s' "$body" | grep -oE '"countryCode"[[:space:]]*:[[:space:]]*"[A-Z]{2}"' \
    | head -1 | grep -oE '[A-Z]{2}' | tail -1
}

# 测一个 mirror 是否能实际连通（HEAD 注册中心 API）
_mirror_reachable() {
  local mirror="$1"
  # docker.1ms.run 这种 mirror 走 https，探活时直接请求 /v2/
  curl -fsS --max-time 5 -o /dev/null -w '%{http_code}' \
    "${mirror%/}/v2/" 2>/dev/null | grep -qE '^(200|401|403)$'
}

# 测一组 mirror，挑出能用的（保序），写到 daemon.json 的 registry-mirrors
_pick_mirrors() {
  local out=()
  for m in "${_CN_MIRRORS[@]}"; do
    if _mirror_reachable "$m"; then
      out+=("$m")
    fi
  done
  printf '%s\n' "${out[@]:-+${_CN_MIRRORS[0]}}" | head -10
}

# 写入 /etc/docker/daemon.json，浅合并保留用户已有的配置
_write_mirrors() {
  [ "${WCM_SKIP_MIRROR:-0}" = "1" ] && { warn "WCM_SKIP_MIRROR=1，跳过"; return 0; }
  mkdir -p /etc/docker
  local daemon_json="/etc/docker/daemon.json"

  log "探测国内 mirror 连通性..."
  local mirrors
  mirrors="$(_pick_mirrors | head -5)"
  if [ -z "$mirrors" ]; then
    warn "没有可用的 mirror（仍按用户已有 daemon.json 跑）"
    return 0
  fi

  # 拼成 JSON 数组
  local mlist=""
  while IFS= read -r m; do
    [ -z "$m" ] && continue
    if [ -z "$mlist" ]; then mlist="\"$m\""
    else mlist="$mlist, \"$m\""
    fi
  done <<< "$mirrors"

  if command -v python3 >/dev/null 2>&1; then
    python3 - <<PY
import json
p = "/etc/docker/daemon.json"
try:
    with open(p, "r", encoding="utf-8") as f:
        cfg = json.load(f)
except Exception:
    cfg = {}
cfg["registry-mirrors"] = [${mlist}]
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print("ok")
PY
    ok "已写入 /etc/docker/daemon.json："
    while IFS= read -r m; do
      [ -z "$m" ] && continue
      printf "    • %s\n" "$m"
    done <<< "$mirrors"
  else
    if [ ! -f "$daemon_json" ]; then
      cat > "$daemon_json" <<JSON
{
  "registry-mirrors": [${mlist}]
}
JSON
      ok "已生成 /etc/docker/daemon.json"
    else
      warn "未检测到 python3，无法合并到现有 daemon.json，跳过"
      return 0
    fi
  fi

  # 触发 reload（docker 已在跑时）
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active docker >/dev/null 2>&1; then
      systemctl reload docker 2>/dev/null && ok "dockerd 已 reload" \
        || warn "dockerd reload 失败（如首次安装可忽略）"
    fi
  fi
}

configure_docker_mirrors() {
  # 已显式跳过
  [ "${WCM_SKIP_MIRROR:-0}" = "1" ] && { warn "WCM_SKIP_MIRROR=1，跳过镜像源配置"; return 0; }
  # 强制写
  if [ "${WCM_FORCE_MIRROR:-0}" = "1" ]; then
    warn "WCM_FORCE_MIRROR=1：强制写入国内镜像源"
    _write_mirrors
    return 0
  fi

  log "检测服务器所在国家（是否需要 Docker 国内镜像源）..."
  local ip country=""
  ip="$(_detect_public_ip || true)"
  if [ -z "$ip" ]; then
    warn "无法获取公网 IP（可能无外网），跳过自动配置（可手动：bash install.sh mirror）"
    return 0
  fi
  country="$(_detect_country "$ip" || true)"
  log "  出口 IP: $ip  归属地: ${country:-?}"

  case "$country" in
    CN|HK|MO)
      log "检测到国内/港澳出口 → 配置 Docker registry-mirrors"
      _write_mirrors
      ;;
    "")
      warn "归属地识别失败 → 跳过（可手动：bash install.sh mirror）"
      ;;
    *)
      log "归属地 $country → 跳过 Docker 镜像源配置（国外机器不需要）"
      ;;
  esac
}

do_mirror() {
  # 单独运行镜像源配置：无需 docker 安装
  [ "${WCM_FORCE_MIRROR:-0}" = "1" ] || [ "${WCM_SKIP_MIRROR:-0}" = "1" ] \
    && { _write_mirrors; return 0; }
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
    local inst_url="https://get.docker.com"
    if [ "${WCM_FORCE_MIRROR:-0}" = "1" ]; then
      local alt="https://get.daocloud.io/docker-ce"
      if curl -fsSI --max-time 6 "$alt" >/dev/null 2>&1; then
        inst_url="$alt"
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

# 智能 pull：失败时回退到国内 mirror，再 re-tag 成原名。
# 用法：smart_pull <full-image-ref> [ <fallback-mirror> ... ]
smart_pull() {
  local target="$1"; shift
  if docker pull "$target" 2>/dev/null; then return 0; fi
  [ "${WCM_NO_MIRROR_FALLBACK:-0}" = "1" ] && { err "拉取 $target 失败（已禁用兜底）"; }

  warn "从 $target 直接拉取失败，尝试国内 mirror 兜底..."
  # 把 kaoqy666/weibo-checkin:v1.2.0 → kaoqy666/weibo-checkin:v1.2.0
  # mirror 源格式：<mirror>/<original-image>
  local mirrors=("$@")
  if [ ${#mirrors[@]} -eq 0 ]; then
    mirrors=("${_CN_MIRRORS[@]}")
  fi
  for m in "${mirrors[@]}"; do
    log "  尝试 $m/$target ..."
    if docker pull "$m/$target" 2>/dev/null; then
      docker tag "$m/$target" "$target"
      ok "  从 $m 拉取成功并 re-tag 为 $target"
      return 0
    fi
  done
  err "所有 mirror 都拉不下来 $target，请检查网络或镜像名"
}

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

do_install() {
  log "微博签到管理面板 · 一键部署"
  log "  端口: $PORT | 数据目录: $DATA_DIR | 镜像: $IMAGE"
  ensure_docker
  configure_docker_mirrors   # 在首次 pull 之前（写入 + reload）
  ensure_compose

  log "拉取镜像 ${IMAGE} ..."
  smart_pull "$IMAGE"

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

do_update() {
  log "更新微博签到面板 → $IMAGE"
  command -v docker >/dev/null 2>&1 || err "未安装 Docker，请先运行 bash install.sh"
  configure_docker_mirrors
  ensure_compose

  log "拉取最新镜像 ..."
  smart_pull "$IMAGE"

  log "停止并删除旧容器 ..."
  docker stop weibo-checkin 2>/dev/null || true
  docker rm weibo-checkin 2>/dev/null || true

  log "启动新容器（数据在 $DATA_DIR，不会丢失）..."
  docker compose -f "$COMPOSE_FILE" up -d
  wait_health

  NEW_VER="$(docker exec weibo-checkin sh -c 'echo ok' >/dev/null 2>&1 && \
    curl -sf "http://127.0.0.1:${PORT}/api/health" 2>/dev/null | \
    sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
  ok "更新完成！当前版本: v${NEW_VER:-?}"
  ok "管理面板: http://<服务器IP>:${PORT}"
}

do_status() {
  docker ps --filter name=weibo-checkin \
    --format '状态: {{.Status}} | 镜像: {{.Image}} | 端口: {{.Ports}}'
  echo "镜像版本: $(docker exec weibo-checkin cat /app/app/main.py 2>/dev/null | \
    sed -n 's/.*version="\([^"]*\)".*/\1/p' || echo '容器未运行')"
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