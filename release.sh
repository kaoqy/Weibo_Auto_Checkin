#!/usr/bin/env bash
# ============================================================
# 创建 GitHub Release（含源码包 + 说明）
#
# 依赖：GITHUB_TOKEN 环境变量（需 repo 权限），curl
# 用法：
#   export GITHUB_TOKEN=ghp_xxx
#   bash release.sh v1.2.0 "Release 说明"
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

TAG="${1:-v1.2.0}"
NOTES="${2:-微博签到管理面板 Release}"
REPO="kaoqy/Weibo_Auto_Checkin"

if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "❌ 需要 GITHUB_TOKEN（repo 权限）"
  echo "   生成: https://github.com/settings/tokens → 勾选 repo"
  exit 1
fi

API="https://api.github.com/repos/$REPO"

echo "▶ 构建源码包..."
SRC="/tmp/weibo-checkin-${TAG}.tar.gz"
tar --exclude='.venv' --exclude='data' --exclude='.git' --exclude='*.db*' \
    --exclude='__pycache__' --exclude='.env' --exclude='_*.py' \
    -czf "$SRC" .

echo "▶ 创建 Release $TAG ..."
JSON=$(cat <<EOF
{"tag_name":"$TAG","name":"$TAG","body":$(printf '%s' "$NOTES" | sed 's/"/\\"/g' | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))'),"draft":false,"prerelease":false}
EOF
)
RESP=$(curl -sf -X POST "$API/releases" -H "Authorization: token $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" -d "$JSON") || { echo "创建 release 失败（可能 tag 已存在？改用现有）"; \
  RESP=$(curl -sf -X POST "$API/releases" -H "Authorization: token $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" -d "$JSON") || { echo "❌ 失败"; exit 1; }; }
RELEASE_ID=$(printf '%s' "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "  release id: $RELEASE_ID"

echo "▶ 上传源码包..."
UPLOAD_URL="https://uploads.github.com/repos/$REPO/releases/$RELEASE_ID/assets?name=weibo-checkin-${TAG}.tar.gz"
curl -sf -X POST "$UPLOAD_URL" -H "Authorization: token $GITHUB_TOKEN" \
     -H "Content-Type: application/gzip" --data-binary @"$SRC" >/dev/null && echo "  ✔ 已上传" || echo "  ⚠️ 上传失败"

echo "🎉 Release 创建完成: https://github.com/$REPO/releases/tag/$TAG"
