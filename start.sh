#!/usr/bin/env bash
# 微博签到管理面板 - 一键部署 / 启动脚本
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"

echo "======================================"
echo "  微博超话签到管理面板"
echo "======================================"

# 1. 若没有虚拟环境则创建
if [ ! -x "$PY" ]; then
  echo "▶ 创建虚拟环境..."
  python3 -m venv --without-pip .venv 2>/dev/null || python3 -m venv .venv
  # 若 venv 缺 pip，用 get-pip 补
  if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    echo "▶ 安装 pip..."
    curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip-wcm.py
    .venv/bin/python /tmp/get-pip-wcm.py
  fi
fi

# 2. 安装依赖
echo "▶ 安装依赖..."
"$PY" -m pip install -q -r requirements.txt 2>/dev/null || true

# 3. 启动
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
echo "▶ 启动服务: http://$HOST:$PORT"
echo "   (Ctrl+C 停止)"
exec "$PY" run.py --host "$HOST" --port "$PORT"
