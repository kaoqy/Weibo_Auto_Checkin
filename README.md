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
- 🌍 **智能代理调度**：Socks5 节点填链接后自动识别归属地（ip-api）；每个账号可指定 socks；**不同 socks 的账号并行签到**，同 socks 依次签到
- 📲 **Telegram 推送**：签到完成自动推送汇总
- 📜 **分组日志**：按日期分区，单次执行的所有账号归并一组
- 🗄️ **SQLite**：账号 / 日志 / 任务 / 用户 / 通知全部持久化
- 🐳 **Docker 一键部署**，镜像自动加版本 tag、保留最近 5 个

## 🚀 快速部署

**方式一：Docker 一键（推荐）**

```bash
bash install.sh 8000
# 或仅用镜像：
docker run -d --name weibo-checkin --restart unless-stopped \
  -p 8000:8000 -v /opt/weibo-checkin/data:/app/data \
  kaoqy666/weibo-checkin:latest
```

完成后访问 `http://<服务器IP>:8000` → **首次进入初始化页设置管理员账号**。

**方式二：本地运行**

```bash
cd weibo-checkin-manager
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

## 🐳 构建与推送（带版本 tag）

```bash
export REGISTRY_USER=你的DockerHub用户名
export REGISTRY_TOKEN=***
bash push.sh [备注]   # 生成 日期-序号 tag（如 20260815-01），更新 latest，保留最近 5 个
```

支持 Docker Hub / GHCR / 私有 registry（按镜像前缀自动判断）。

## 🔒 安全

- 登录保护默认开启；密码 PBKDF2 哈希存储
- Cookie 存本地 SQLite，勿公开 `data/` 目录
- 建议反向代理 + HTTPS 访问

## 🧪 测试

```bash
.venv/bin/python -m pytest tests/ -v   # 41 个测试
node tests/frontend-render.test.js      # 前端渲染测试（自动装 jsdom）
```

## 📂 结构

```
weibo-checkin-manager/
├── run.py            # 启动
├── install.sh        # 一键部署
├── push.sh           # 构建+推送(带 tag)
├── Dockerfile
├── app/              # 后端 + 前端
│   ├── main.py       # FastAPI 入口 + 认证中间件
│   ├── weibo_client.py / weibo_login.py
│   ├── scheduler.py  # 定时 + 防封编排
│   ├── notifier.py   # TG 推送
│   └── static/       # 前端页面
└── tests/
```

## 📄 License

MIT
