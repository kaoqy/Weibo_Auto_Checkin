# 微博签到管理面板 · 常用命令（开发用）
#
# **终端用户不需要这个文件**——使用 `bash install.sh` 即可。
# 镜像构建与推送由 GitHub Actions 在 push tag 后自动完成。
#
# 本 Makefile 仅供本地开发与测试。

.PHONY: run checkin test test-frontend docker-build docker-up docker-down clean

# 本地启动 Web 面板
run:
	.venv/bin/python run.py

# 命令行跑一次签到
checkin:
	.venv/bin/python run.py checkin

# 运行后端测试
test:
	.venv/bin/python -m pytest tests/ -v

# 运行前端渲染测试
test-frontend:
	node tests/frontend-render.test.js

# 本地构建 Docker 镜像（不需要推送）
docker-build:
	docker build -t weibo-checkin:latest .

# Docker Compose 本地起（build + run）
docker-up:
	docker compose up -d --build

# 停本地 compose
docker-down:
	docker compose down

# 清理测试产物
clean:
	rm -rf .pytest_cache __pycache__ app/**/__pycache__