#!/usr/bin/env bash
# ============================================================
# 微博签到管理面板 · 构建 + 推送到 Docker Hub（带版本 tag）
#
# 每次推送生成 日期-序号 的 tag（如 20260815-01），并更新 latest。
# 通过 Docker Hub API 清理旧 tag，仅保留最近 5 个 + latest。
#
# 用法（在有 Docker 的机器上）:
#   bash push.sh [备注]
#   备注会拼进 tag（可选，如 bash push.sh "fix-tg" → 20260815-01-fix-tg）
#
# 读取 .env 里的 REGISTRY_USER / REGISTRY_TOKEN / WCM_IMAGE
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

# ---- 读取配置 ----
if [ -f .env ]; then
  set -a; source .env; set +a
fi

IMAGE_BASE="${WCM_IMAGE_BASE:-kaoqy666/weibo-checkin}"   # 不含 tag
REGISTRY="${REGISTRY:-docker.io}"
USER="${REGISTRY_USER:-}"
TOKEN="${REGISTRY_TOKEN:-}"
KEEP="${KEEP_TAGS:-5}"        # 保留几个版本 tag（不含 latest）
NOTE="${1:-}"                 # 可选备注

# 生成日期-序号 tag
DATE_TAG="$(date +%Y%m%d)"
# 查询远端该日期已有最大序号
SEQ=1
MAXSEQ=""
if [ -n "$TOKEN" ]; then
  JWT_TMP="$(curl -sf -X POST https://hub.docker.com/v2/users/login/ -H 'Content-Type: application/json' -d "{\"username\":\"$USER\",\"password\":\"$TOKEN\"}" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
  if [ -n "$JWT_TMP" ]; then
    TAGS_JSON="$(curl -sf "https://hub.docker.com/v2/repositories/$USER/${IMAGE_BASE##*/}/tags?page_size=100" -H "Authorization: JWT $JWT_TMP" 2>/dev/null || echo '{}')"
    MAXSEQ=$(printf '%s' "$TAGS_JSON" | grep -oE "${DATE_TAG}-[0-9]+" | grep -oE '[0-9]+$' | sort -n | tail -1)
  fi
fi
if [ -n "$MAXSEQ" ]; then SEQ=$((MAXSEQ + 1)); fi

VERSION_TAG="${DATE_TAG}-$(printf '%02d' "$SEQ")"
if [ -n "$NOTE" ]; then
  # 备注清洗：只留字母数字下划线连字符
  NOTE_CLEAN="$(printf '%s' "$NOTE" | tr -cd '[:alnum:]_-' | head -c 20)"
  [ -n "$NOTE_CLEAN" ] && VERSION_TAG="${VERSION_TAG}-${NOTE_CLEAN}"
fi

IMAGE="$IMAGE_BASE:$VERSION_TAG"
IMAGE_LATEST="$IMAGE_BASE:latest"

echo "======================================"
echo "  微博签到面板 · Docker 构建+推送"
echo "  版本 tag: $VERSION_TAG"
echo "  (保留最近 $KEEP 个版本，latest 始终更新)"
echo "======================================"

# ---- 前置检查 ----
if ! command -v docker >/dev/null 2>&1; then
  echo "❌ 未找到 docker，请先安装 Docker： https://docs.docker.com/get-docker/"
  exit 1
fi
if [ -z "$USER" ] || [ -z "$TOKEN" ]; then
  echo "❌ 缺少 REGISTRY_USER / REGISTRY_TOKEN（编辑 .env）"
  exit 1
fi

# ---- 登录 ----
echo "▶ 登录 $REGISTRY ($USER) ..."
echo "$TOKEN" | docker login "$REGISTRY" -u "$USER" --password-stdin
echo "✔ 登录成功"

# ---- 构建（版本 tag + latest）----
echo "▶ 构建 $IMAGE ..."
docker build -t "$IMAGE" -t "$IMAGE_LATEST" .
echo "✔ 构建完成"

# ---- 推送 ----
echo "▶ 推送... "
docker push "$IMAGE"
docker push "$IMAGE_LATEST"
echo "✔ 推送完成"

# ---- 清理旧 tag（保留最近 KEEP 个版本）----
echo "▶ 清理旧版本 tag（保留最近 $KEEP 个）..."
# 获取 JWT
JWT="$(curl -sf -X POST https://hub.docker.com/v2/users/login/ -H 'Content-Type: application/json' -d "{\"username\":\"$USER\",\"password\":\"$TOKEN\"}" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
if [ -n "$JWT" ]; then
  REPO="$USER/${IMAGE_BASE##*/}"
  # 列出所有 tag（按更新时间倒序）
  TAGS="$(curl -sf "https://hub.docker.com/v2/repositories/$REPO/tags?page_size=100&ordering=last_updated" -H "Authorization: JWT $JWT" 2>/dev/null || echo '{}')"
  # 提取版本 tag（排除 latest），取最新的删除较旧的
  ALL_TAGS="$(printf '%s' "$TAGS" | grep -oE '"name":"[^"]*"' | sed 's/"name":"//;s/"$//' | grep -vE '^latest$' || true)"
  # 按版本 tag 字母序（YYYYMMDD-NN 可排序）取最新的 KEEP 个保留,其余删
  KEEP_THIS="$((KEEP))"
  TO_DELETE="$(printf '%s\n' $ALL_TAGS | sort -r | tail -n +$((KEEP_THIS+1)))"
  if [ -n "$TO_DELETE" ]; then
    echo "$TO_DELETE" | while read -r tag; do
      [ -z "$tag" ] && continue
      echo "  删除旧 tag: $tag"
      curl -sf -X DELETE "https://hub.docker.com/v2/repositories/$REPO/tags/$tag" -H "Authorization: JWT $JWT" >/dev/null 2>&1 || echo "    (删除失败，可能已被删)"
    done
  else
    echo "  (无需清理，现有 tag 少于等于 $KEEP 个)"
  fi
else
  echo "  ⚠️ 无法获取 Docker Hub API token，跳过清理（不影响推送）"
fi

echo ""
echo "🎉 完成！"
echo "  版本镜像: $IMAGE"
echo "  latest:   $IMAGE_LATEST"
echo "  服务器拉取示例: docker pull $IMAGE_LATEST"
