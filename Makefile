# 微博签到管理面板 · 常用命令
.PHONY: run checkin test test-frontend docker-build docker-up docker-down deploy deploy-remote clean

# 本地启动
run:
	.venv/bin/python run.py

# 命令行跑一次签到
checkin:
	.venv/bin/python run.py checkin

# 运行测试
test:
	.venv/bin/python -m pytest tests/ -v

test-frontend:
	node tests/frontend-render.test.js

# Docker 本地构建
docker-build:
	docker build -t weibo-checkin:latest .

# Docker Compose 启动（构建+运行）
docker-up:
	docker compose up -d --build

# Docker Compose 停止
docker-down:
	docker compose down

# 登录+构建+推送+远程部署
deploy:
	bash deploy.sh deploy

# 仅远程部署（假定已推送）
deploy-remote:
	bash deploy.sh remote

# 清理测试产物
clean:
	rm -rf .pytest_cache __pycache__ app/**/__pycache__
