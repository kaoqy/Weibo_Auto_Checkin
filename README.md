# 微博超话签到管理面板

一个**带 Web 管理面板**的微博超话自动签到系统。基于 FastAPI + SQLite 构建，支持多账号管理、自动定时签到、Telegram 通知推送、SOCKS5 代理池与防封策略。

> 基于原有青龙单文件脚本（`../Weibo_Checkin/weibo_checkin.py`）的核心签到逻辑重写，封装成带数据库、调度器和可视化界面的完整应用。

---

## ✨ 功能特性

| 模块 | 说明 |
| --- | --- |
| 🖥️ **Web 管理面板** | 仪表盘、账号管理、签到日志、设置，现代化响应式 UI（白天/黑夜主题） |
| 👥 **多账号管理** | 添加/编辑/删除账号，Cookie 存储与校验，启用/停用开关 |
| ⏰ **自动定时签到** | APScheduler 定时任务，Cron 表达式可配置，默认每天 7:00 |
| 📲 **Telegram 推送** | 签到完成自动推送汇总报告，支持测试推送 |
| 🛡️ **防封策略** | 账号间随机等待（凌晨窗口），SOCKS5 代理池负载均衡、失败回退直连、三遍重试 |
| 🔄 **Cookie 自动续命** | 自动合并微博服务端下发的 Set-Cookie 并回写 |
| 🗄️ **SQLite 数据库** | 单文件存储，账号/日志/任务/通知记录均持久化 |
| 🧪 **完整测试** | 30 个单元/集成测试，覆盖数据库、客户端逻辑、API 接口 |

---

## 📁 项目结构

```
weibo-checkin-manager/
├── run.py                  # 启动脚本（web / 命令行跑一次签到）
├── requirements.txt        # Python 依赖
├── Dockerfile              # 多阶段构建镜像
├── docker-compose.yml      # 本地/开发 compose（含 build）
├── compose.prod.yml        # 生产 compose（仅拉取镜像）
├── deploy.sh               # 一键：登录→构建→推送→远程部署
├── .env.example            # 部署环境变量模板
├── Makefile                # 常用命令入口
├── app/
│   ├── main.py             # FastAPI 入口（初始化 DB + 调度器 + 静态托管）
│   ├── database.py         # SQLite 数据库层（线程安全）
│   ├── weibo_client.py     # 微博签到核心逻辑（Cookie 解析/超话列表/签到）
│   ├── scheduler.py        # 签到调度与执行编排（手动 + 定时）
│   ├── notifier.py         # Telegram 通知推送
│   ├── anti_ban.py         # 防封策略（随机等待 / 节点轮换）
│   ├── api/
│   │   ├── accounts.py     # 账号 CRUD + Cookie 校验 API
│   │   └── tasks.py        # 任务触发、日志、设置 API
│   └── static/             # 前端管理面板（index.html / style.css / app.js）
├── tests/                  # pytest 测试套件
└── data/                   # 运行时自动创建（SQLite 数据库文件）
```

---

## 🚀 快速开始

### 方式一：本地直接运行

#### 1. 创建虚拟环境并安装依赖

```bash
cd weibo-checkin-manager
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# 若系统缺 pip，先执行：python3 /tmp/get-pip.py 或用 --without-pip + get-pip
```

#### 2. 启动服务

```bash
# 启动 Web 管理面板（默认 http://0.0.0.0:8000）
.venv/bin/python run.py

# 指定端口 / 仅本机访问
.venv/bin/python run.py --port 9000 --host 127.0.0.1
```

浏览器打开 `http://localhost:8000` 进入管理面板。

#### 3. 命令行直接跑一次签到（不启动 Web）

```bash
.venv/bin/python run.py checkin
```

---

## 🐳 Docker 一键部署

### 方式二：Docker Compose（本地/服务器）

```bash
cd weibo-checkin-manager
cp .env.example .env      # 可选：修改端口/镜像名

docker compose up -d --build
```

数据（SQLite 数据库）持久化在宿主机的 `./data` 目录，容器重建不丢失。

### 构建并推送到镜像仓库 + 远程部署

```bash
# 1) 配置仓库凭据（推荐环境变量，不落盘）
export REGISTRY_USER=yourname
export REGISTRY_TOKEN=your_token_or_password

# 2) 在 .env 里设置目标镜像名，例如：
#    WCM_IMAGE=docker.io/yourname/weibo-checkin
#    WCM_IMAGE=ghcr.io/yourname/weibo-checkin
#    WCM_DEPLOY_HOST=root@1.2.3.4

# 3) 一键：登录 → 构建 → 推送 → ssh 远程部署
bash deploy.sh deploy

# 可选子命令：
#   bash deploy.sh build   # 仅本地构建
#   bash deploy.sh push    # 登录+构建+推送（不远程部署）
#   bash deploy.sh remote  # 仅远程部署（假定已推送）
```

镜像仓库支持：**Docker Hub**（`docker.io/...`）、**GitHub Container Registry**（`ghcr.io/...`）、**私有 registry**（`registry.example.com/...`），脚本会自动按镜像名前缀判断并执行对应 `docker login`。

### 服务器端独立部署（compose.prod.yml）

若不想用 `deploy.sh remote`，可在服务器上直接：

```bash
# 服务器上先推好/拉好镜像
export WCM_IMAGE=yourname/weibo-checkin:latest
export WCM_DATA_DIR=/opt/weibo-checkin/data
mkdir -p /opt/weibo-checkin/data

docker compose -f compose.prod.yml up -d
```

---

## 🎯 使用流程

1. **添加账号**：进入「账号管理」→「添加账号」，填入账号名称和微博 Cookie（浏览器登录 m.weibo.cn 后从开发者工具复制）。
2. **校验 Cookie**：点击账号行的「校验」按钮确认 Cookie 有效。
3. **配置设置**：在「设置」页配置 TG 推送（Bot Token + Chat ID）、定时表达式、防封参数、代理节点。
4. **开始签到**：点击顶栏「立即签到」手动触发，或等待定时任务自动执行。

---

## ⚙️ 设置说明

### Telegram 通知

| 配置项 | 说明 |
| --- | --- |
| `tg_enabled` | 是否启用 TG 推送 |
| `tg_bot_token` | 从 @BotFather 创建的 Bot Token |
| `tg_user_id` | 接收通知的 Chat/User ID |

### 定时签到

- `schedule_enabled`：是否启用定时任务
- `schedule_cron`：Cron 表达式，默认 `0 7 * * *`（每天 7:00）。支持标准 5 段或 6 段 cron。

### 防封策略

| 配置项 | 说明 |
| --- | --- |
| `anti_ban_enabled` | 总开关 |
| `anti_ban_wait_min` / `anti_ban_wait_max` | 账号间随机等待秒数（默认 120~300s） |
| `anti_ban_window_hour` | 仅在凌晨该小时前启用等待（默认 7，即 0~6 点生效） |

### 网络 / 代理

- `proxies`：SOCKS5 节点列表（每行一个），如 `socks5://user:pass@host:port`
- `proxy_force`：严格代理（失败不回退直连）
- `proxy_fallback`：允许失败回退直连

### 签到参数

- `checkin_delay_min` / `checkin_delay_max`：超话之间随机延时（防并发被风控）

---

## ✅ 运行测试

### Python 后端测试

```bash
.venv/bin/python -m pytest tests/ -v
```

覆盖：数据库增删改查、设置读写、Cookie 解析、代理解析、防封策略、节点轮换、账号 CRUD API、设置/日志/统计 API、Cookie 校验 API、完整签到流程（mock）、静态资源托管。

### 前端渲染测试（需 node）

```bash
node tests/frontend-render.test.js
```

用 jsdom 加载真实 `index.html` + `app.js`（mock fetch，jsdom 缺失时会自动临时安装），验证仪表盘统计卡、导航、账号表格、设置表单、弹窗是否正确渲染。能捕获如“元素 ID 写错”这类只在运行时才暴露的 bug。

---

## 🔒 安全说明

- Cookie 明文存储在本地 SQLite，请勿将 `data/` 目录提交到代码仓库或公网暴露。
- 建议服务仅监听本机（`--host 127.0.0.1`）或置于反向代理/CORS 白名单后面。
- 多账号高频操作有被封号风险，请合理设置防封等待与延时。

---

## 🐛 常见问题

**Q: 签到提示 "Cookie 无效或已过期"？**
Cookie 失效了。重新登录微博，更新账号的 Cookie 后再校验。

**Q: 如何获取微博 Cookie？**
电脑浏览器登录 https://m.weibo.cn ，F12 → Application/存储 → Cookies，复制全部键值对（或直接粘贴完整 Cookie 字符串）。

**Q: 定时任务没触发？**
检查「设置」里 `schedule_enabled` 是否为开、Cron 表达式是否合法（可参考 crontab.guru）。

**Q: 代理节点如何配置？**
在「设置 → 网络/代理」的文本框里每行填一个 `socks5://` 开头的节点，保存即可。
