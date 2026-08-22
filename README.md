# 微博超话签到管理面板

带 **Web 管理面板**的微博超话自动签到系统。FastAPI + SQLite，支持扫码登录、自动定时签到、Telegram 通知、SOCKS5 代理池与防封，可 Docker 一键部署。

## ✨ 功能

- 🖥️ **管理面板**：仪表盘 / 账号 / 日志 / 设置，白天黑夜主题
- 🔐 **登录保护**：首次部署可视化初始化设置管理员；未登录拦截；改密 / 退出
- 📱 **微博扫码添加账号**：弹二维码 → 微博 App 扫码确认 → 自动获取 Cookie
- 🍪 **Cookie 生成器**：粘贴 Cookie 自动解析，一键导入账号
- ⏰ **自动定时签到**：Cron 可配（支持 5 段 / 6 段青龙格式）
- ✅ **已签自动跳过**：不重复签到
- 🛡️ **防封策略**：凌晨窗口随机等待、SOCKS5 代理池、失败回退、三遍重试
- 🌍 **智能代理调度**：独立「代理」页管理 Socks5 节点（手动输入或粘贴链接自动识别归属地）；每个账号可指定 socks；**不同 socks 的账号并行签到**，同 socks 依次签到
- 📲 **Telegram 推送**：签到完成自动推送汇总
- 📜 **分组日志**：按日期分区，单次执行的所有账号归并一组
- 🗄️ **SQLite**：账号 / 日志 / 任务 / 用户 / 通知全部持久化
- 🐳 **Docker 一键部署**

## 🚀 快速部署

方式一：**App 镜像一键部署（推荐，无需源码）**

```bash
# 下载脚本后运行
curl -o install.sh https://raw.githubusercontent.com/kaoqy/Weibo_Auto_Checkin/main/install.sh
chmod +x install.sh
bash install.sh            # 默认端口 8000
bash install.sh 8080       # 指定端口
```

脚本会自动：安装 Docker → 拉取镜像 → 启动容器 → 等待健康。完成后访问 `http://<服务器IP>:<端口>`，**首次进入初始化页设置管理员账号密码**。


## 🔄 更新到最新版

数据持久化在 `data/` 卷里，更新不会丢账号/日志/配置：

```bash
bash install.sh update
```

## 🧰 日常管理

```bash
bash install.sh status     # 查看容器状态与版本（无需 root）
bash install.sh logs       # 跟随查看日志
bash install.sh start      # 启动
bash install.sh stop       # 停止
bash install.sh restart    # 重启
```

## 🐳 手动 Docker（不用脚本）

```bash
# 安装
docker run -d --name weibo-checkin --restart unless-stopped \
  -p 8000:8000 -v /opt/weibo-checkin/data:/app/data \
  kaoqy666/weibo-checkin:latest

# 更新
docker pull kaoqy666/weibo-checkin:latest
docker rm -f weibo-checkin
docker run -d --name weibo-checkin --restart unless-stopped \
  -p 8000:8000 -v /opt/weibo-checkin/data:/app/data \
  kaoqy666/weibo-checkin:latest

# 确认
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/health  # 期望 200
```

> Docker Hub 会同时发布带日期-迭代的版本 tag（如 `kaoqy666/weibo-checkin:20260816-01`），`latest` 永远指向最新。

## 🛠️ 开发者：本地构建 / 发布（deploy.sh）

`deploy.sh` 面向**维护者**：构建镜像 → 推送 Docker Hub →（可选）SSH 远程部署。

```bash
# 环境变量（或 .env 文件，含 REGISTRY_USER / REGISTRY_TOKEN）
bash deploy.sh push          # 登录 → 构建 → 推送（latest + 日期tag，保留最近5个）
bash deploy.sh build         # 仅本地构建
bash deploy.sh remote        # 仅远程部署（需已推送 + WCM_DEPLOY_HOST）
bash deploy.sh deploy        # 构建+推送+远程部署
```

本地源码运行（开发调试）：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py          # http://localhost:8000
.venv/bin/python run.py checkin  # 命令行跑一次签到
```

## 🎯 使用

1. 初始化/登录面板
2. 「设置 → 网络/代理」填 SOCKS5 节点（每行一个）→ 点「识别归属地」查看每个节点的国家/地区
3. 添加账号时可为每个账号「指定 socks 节点」（下拉显示归属地）
   - **不同 socks 的账号 → 并行签到**
   - **同 socks / 未指定的账号 → 依次签到**
4. 「设置」配 TG 通知、定时、防封
5. 点「立即签到」或等定时任务自动执行

## 🔒 安全

- 登录保护默认开启；密码 PBKDF2 哈希存储
- Cookie 存本地 SQLite，勿公开 `data/` 目录
- 建议反向代理 + HTTPS 访问

## 🧪 测试

```bash
.venv/bin/python -m pytest tests/ -v   # 57 个测试
node tests/frontend-render.test.js      # 前端渲染测试（自动装 jsdom）
```

## 📂 结构

```
weibo-checkin-manager/
├── run.py            # 本地启动
├── install.sh        # 用户一键安装/更新/管理（推荐）
├── deploy.sh         # 维护者：构建/推送/远程部署
├── release.sh        # 创建 GitHub Release
├── compose.prod.yml  # 生产 compose（仅拉镜像运行）
├── Dockerfile
├── app/              # 后端 + 前端
│   ├── main.py       # FastAPI 入口 + 认证中间件
│   ├── weibo_client.py
│   ├── proxy_geo.py  # SOCKS5 代理归属地识别
│   ├── scheduler.py  # 定时 + 防封 + 分组并行调度
│   ├── notifier.py   # TG 推送
│   ├── api/          # accounts / proxies / tasks / auth
│   └── static/       # 前端页面（含代理管理页）
└── tests/
```

## 📄 License

MIT
