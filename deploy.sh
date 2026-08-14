#!/usr/bin/env bash
# ============================================================
# 微博签到管理面板 · Docker 一键部署脚本
#
# 功能：
#   - 登录镜像仓库（Docker Hub / GHCR / 私有 registry）
#   - 构建镜像
#   - 推送镜像到仓库
#   - （可选）ssh 到远程服务器一键拉取并部署
#
# 用法：                bash deploy.sh
#   仅构建+推送：       bash deploy.sh push
#   构建+推送+远程部署： bash deploy.sh deploy
#   仅远程部署(已推送)： bash deploy.sh remote
#
# 依赖： docker, （远程部署时）ssh / scp
# 密钥： 两种方式任选
#   1) 环境变量 REGISTRY_USER / REGISTRY_TOKEN（推荐, 不落盘）
#   2) 交互输入（脚本会提示）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

# ---------- 读取配置 ----------
if [ -f .env ]; then
  set -a; source .env; set +a
fi

IMAGE="${WCM_IMAGE:-weibo-checkin:latest}"
DEPLOY_HOST="${WCM_DEPLOY_HOST:-}"
REMOTE_IMAGE="${WCM_REMOTE_IMAGE:-$IMAGE}"
REMOTE_DATA="${WCM_REMOTE_DATA:-/opt/weibo-checkin/data}"

REGISTRY_USER="${REGISTRY_USER:-}"
REGISTRY_TOKEN="${REGISTRY_TOKEN:-}"

# 从镜像名推导 registry 地址（docker.io / ghcr.io / 其他）
case "$IMAGE" in
  ghcr.io/*)  REGISTRY="ghcr.io" ;;
  docker.io/*|*/weibo-checkin*) REGISTRY="docker.io" ;;
  */*)        REGISTRY="${IMAGE%%/*}" ;;   # 私有 registry: host/ns/img
  *)          REGISTRY="docker.io" ;;
esac

log()  { printf "\033[1;34m▶\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m✔\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m✘\033[0m %s\n" "$*" >&2; }

# ---------- 登录仓库 ----------
login() {
  if [ -z "$REGISTRY_USER" ] && [ -z "$REGISTRY_TOKEN" ]; then
    log "登录 $REGISTRY （请输入凭据）"
    read -rp "  用户名: " REGISTRY_USER
    read -rsp "  Token/密码: " REGISTRY_TOKEN; echo
  fi
  if [ -z "$REGISTRY_USER" ] || [ -z "$REGISTRY_TOKEN" ]; then
    err "缺少 REGISTRY_USER / REGISTRY_TOKEN"
    err "可用环境变量设置，或确认 .env 已配置。"
    exit 1
  fi
  log "登录 $REGISTRY ..."
  echo "$REGISTRY_TOKEN" | docker login "$REGISTRY" -u "$REGISTRY_USER" --password-stdin
  ok "登录成功"
}

# ---------- 构建+推送 ----------
build_and_push() {
  log "构建镜像 $IMAGE ..."
  docker build -t "$IMAGE" .
  ok "镜像构建完成"

  log "推送镜像 $IMAGE → $REGISTRY ..."
  docker push "$IMAGE"
  ok "镜像已推送"
}

# ---------- 远程部署 ----------
remote_deploy() {
  if [ -z "$DEPLOY_HOST" ]; then
    err "未配置 WCM_DEPLOY_HOST，跳过远程部署"
    err "部署前请先手动推送镜像，然后在服务器上运行 compose，或设置 WCM_DEPLOY_HOST"
    return
  fi
  log "远程部署到 $DEPLOY_HOST ..."
  log "  远程创建数据目录 $REMOTE_DATA"
  ssh "$DEPLOY_HOST" "mkdir -p $REMOTE_DATA"

  log "  远程拉取/启动容器（使用 $REMOTE_IMAGE）"
  ssh "$DEPLOY_HOST" "docker rm -f weibo-checkin 2>/dev/null || true; \
      docker pull $REMOTE_IMAGE; \
      docker run -d --name weibo-checkin --restart unless-stopped \
        -p 8000:8000 \
        -e TZ=Asia/Shanghai \
        -v $REMOTE_DATA:/app/data \
        $REMOTE_IMAGE"
  ok "远程部署完成: http://$DEPLOY_HOST:8000"
}

# ---------- 主流程 ----------
cmd="${1:-push}"
case "$cmd" in
  push)
    login
    build_and_push
    ;;
  deploy)
    login
    build_and_push
    remote_deploy
    ;;
  remote)
    remote_deploy
    ;;
  build)
    log "仅构建镜像 $IMAGE"
    docker build -t "$IMAGE" .
    ok "构建完成"
    ;;
  *)
    echo "用法: bash deploy.sh [push|deploy|remote|build]"
    exit 1
    ;;
esac

ok "全部完成"
