#!/usr/bin/env bash
# ============================================================
# 微博签到管理面板 · 一键构建并推送到 Docker Hub
#
# 用法（在有 Docker 的机器上）:
#   bash push.sh
#
# 会读取 .env 里的 REGISTRY_USER / REGISTRY_TOKEN / WCM_IMAGE
# .env 已被 gitignore，密钥不会提交/泄露。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

# 读取 .env（密钥）
if [ -f .env ]; then
  set -a; source .env; set +a
fi

IMAGE="${WCM_IMAGE:-kaoqy666/weibo-checkin:latest}"
REGISTRY="${REGISTRY:-docker.io}"
USER="${REGISTRY_USER:-}"
TOKEN="${REGISTRY_TOKEN:-}"

echo "======================================"
echo "  微博签到面板 · Docker 构建+推送"
echo "  镜像: $IMAGE"
echo "======================================"

# 前置检查
if ! command -v docker >/dev/null 2>&1; then
  echo "❌ 未找到 docker，请先安装 Docker："
  echo "   https://docs.docker.com/get-docker/"
  exit 1
fi

if [ -z "$USER" ] || [ -z "$TOKEN" ]; then
  echo "❌ 缺少 REGISTRY_USER / REGISTRY_TOKEN"
  echo "   请编辑 .env 填入 Docker Hub 凭据"
  exit 1
fi

# 1. 登录
echo "▶ 登录 $REGISTRY ($USER) ..."
echo "$TOKEN" | docker login "$REGISTRY" -u "$USER" --password-stdin
echo "✔ 登录成功"

# 2. 构建
echo "▶ 构建镜像 $IMAGE ..."
docker build -t "$IMAGE" .
echo "✔ 构建完成"

# 3. 推送
echo "▶ 推送镜像 ..."
docker push "$IMAGE"
echo "✔ 推送完成"

echo ""
echo "🎉 完成！镜像已推送到: $IMAGE"
echo "   服务器部署:  docker pull $IMAGE"
