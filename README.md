# 微博超话签到管理面板

一个**带 Web 管理面板**的微博超话自动签到系统。基于 FastAPI + SQLite，支持扫码登录添加账号、自动定时签到、Telegram 通知、SOCKS5 代理池与防封策略，可 Docker 一键部署。

> ⭐ 本项目是自包含的 Web 应用：集成了微博 m.weibo.cn 扫码登录、Cookie 校验与自动续命、按日期分组的签到日志、以及首次部署的初始化向导。

---

## ✨ 功能特性

| 模块 | 说明 |
| --- | --- |
| 🖥️ **Web 管理面板** | 仪表盘、账号管理、分组日志、设置，现代化响应式 UI（白天/黑夜主题） |
| 🔐 **用户登录 + 初始化向导** | 首次部署自动进入初始化页设置管理员；之后登录保护（未登录跳登录页/API 401）；支持改密、退出、会话 7 天 |
| 📱 **微博扫码登录添加账号** | 点击「扫码添加」弹出二维码 → 用微博 App 扫码确认 → 自动获取 Cookie 并一键添加账号 |
| 🍪 **Cookie 生成器** | 粘贴完整微博 Cookie 自动解析（ALF/SCF/SUB/SUBP/XSRF-TOKEN），可一键导入账号管理或下载 JSON |
| ⏰ **自动定时签到** | APScheduler 定时任务，Cron 可配（支持 5 段标准 / 6 段青龙格式），默认每天 0:10 |
| ✅ **已签自动跳过** | 检测超话"已签/已签到"状态，已签的自动跳过，不重复签到 |
| 🛡️ **防封策略** | 凌晨窗口账号间随机等待、SOCKS5 代理池负载均衡、失败回退直连、三遍重试、超话间随机延时 |
| 📲 **Telegram 推送** | 签到完成自动推送汇总报告，支持测试推送 |
| 📜 **分组日志** | 日志按日期分区（📅 2026-08-14），同一次执行的多个账号归并为一组，直观展示每次任务 |
| 🔄 **Cookie 自动续命** | 自动合并微博服务端下发的 Set-Cookie 并回写数据库 |
| 🗄️ **SQLite 数据库** | 单文件存储，账号/日志/任务/用户/通知全部持久化 |
| 🐳 **Docker 一键部署** | `install.sh` 自动装 Docker + 拉取镜像 + 启动 + 初始化引导 |

---

## 📁 项目结构

```
weibo-checkin-manager/
├── run.py                  # 启动脚本（web / 命令行跑一次签到）
├── requirements.txt        # Python 依赖
├── install.sh              # 🚀 一键部署脚本（装 Docker→拉镜像→启动）
├── Dockerfile              # 多阶段构建镜像
├── docker-compose.yml      # 本地/开发 compose（含 build）
├── compose.prod.yml        # 生产 compose（仅拉取镜像）
├── deploy.sh               # 登录→构建→推送→远程部署
├── push.sh                 # 构建并推送到 Docker Hub
├── .env.example            # 部署环境变量模板
├── app/
│   ├── main.py             # FastAPI 入口（初始化 + 调度器 + 认证中间件 + 静态托管）
│   ├── database.py         # SQLite 数据库层（线程安全）
│   ├── auth.py             # 认证（PBKDF2 密码、会话、初始化）
│   ├── weibo_client.py     # 微博签到核心（Cookie/超话列表/签到/已签跳过/防封）
│   ├── weibo_login.py      # 微博扫码登录（二维码生成/轮询/Cookie 获取）
│   ├── scheduler.py        # 签到调度与执行编排（手动 + 定时）
│   ├── notifier.py         # Telegram 通知推送
│   ├── anti_ban.py         # 防封策略
│   ├── api/
│   │   ├── accounts.py     # 账号 CRUD + Cookie 校验 API
│   │   ├── tasks.py        # 任务/日志/设置 API（含分组日志）
│   │   └── auth.py         # 登录/登出/改密/扫码/初始化 API
│   └── static/             # 前端（index.html/login.html/init.html/cookiegen.html/style.css/app.js）
└── tests/                  # pytest 测试套件（41 个测试）
```

---

## 🚀 快速开始

### 方式一：Docker 一键部署（推荐）

在**装有 Linux 的服务器**上执行（需要 root）:

```bash
# 上传本项目到服务器，或直接：
#   方式A：有源码
bash install.sh 8000
#   方式B：仅用镜像（无需源码）
docker run -d --name weibo-checkin --restart unless-stopped -p 8000:8000 \
  -v /opt/weibo-checkin/data:/app/data -e TZ=Asia/Shanghai \
  kaoqy666/weibo-checkin:latest
```

`install.sh` 会自动：
1. 检测并安装 Docker / docker compose
2. 拉取镜像 `kaoqy666/weibo-checkin:latest`
3. 启动容器（数据持久化到 `./data`）
4. 等待健康检查通过

完成后访问 `http://<服务器IP>:8000` → **首次访问进入初始化页，设置管理员账号密码**。

### 方式二：本地直接运行（开发）

```bash
cd weibo-checkin-manager
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py          # 打开 http://localhost:8000
# 直接跑一次签到（不启动 Web）：
.venv/bin/python run.py checkin
```

---

## 🎯 使用流程

1. **首次部署**：打开面板 → 初始化页设置管理员账号密码（自动登录）。
2. **添加账号**（二选一）：
   - **扫码添加**：账号管理 →「📱 扫码添加」→ 微博 App 扫码确认 → 自动添加。
   - **Cookie 生成器**：顶栏「🍪 Cookie生成器」→ 粘贴微博 Cookie → 一键导入。
3. **校验 Cookie**：账号管理 → 点「校验」确认有效。
4. **配置设置**：「设置」页配 TG 通知、定时、防封参数、代理节点。
5. **开始签到**：顶栏「立即签到」手动触发，或等定时任务自动执行（已签超话自动跳过）。

---

## ⚙️ 设置说明

### Telegram 通知
`tg_enabled` / `tg_bot_token` / `tg_user_id`（从 @BotFather 创建）。

### 定时签到
- `schedule_enabled`：启用定时
- `schedule_cron`：Cron 表达式。支持**标准 5 段**（`10 0 * * *` = 每天 0:10）和**青龙 6 段**（`0 10 0 * * *`，自动去秒）。

### 防封策略
`anti_ban_enabled` / `anti_ban_wait_min` / `anti_ban_wait_max`（账号间随机等待秒）/ `anti_ban_window_hour`（凌晨 N 点前启用）。

### 网络/代理
`proxies`（SOCKS5 节点，每行一个）/ `proxy_force`（严格代理）/ `proxy_fallback`（失败回退直连）。
> 使用 SOCKS5 代理时镜像已内置 PySocks，无需额外安装。

### 签到参数
`checkin_delay_min` / `checkin_delay_max`（超话间随机延时）。

### 账户
修改密码、退出登录。

---

## 📜 日志说明

日志页面按 **日期分区**（📅 2026-08-14），一天内的**每次执行**（同一次手动/定时签到）归并为一组，展示：
- 执行时间、组状态（✅/⚠️/❌）
- 账号数、成功/失败数
- 每个账号的签到详情（超话名、✓/✗）

方便查看"某天某次运行了哪些账号、各自结果如何"。

---

## 🐳 Docker 构建与推送

```bash
# 构建并推送镜像到 Docker Hub（需先登录）
export REGISTRY_USER=你的DockerHub用户名
export REGISTRY_TOKEN=你的AccessToken
bash deploy.sh push          # 登录+构建+推送
bash deploy.sh deploy        # 登录+构建+推送+远程部署（需 WCM_DEPLOY_HOST）

# 或仅推送：
bash push.sh
```

支持 Docker Hub / GHCR / 私有 registry（按镜像名前缀自动判断）。

---

## 🧪 运行测试

```bash
.venv/bin/python -m pytest tests/ -v   # 41 个 Python 测试
node tests/frontend-render.test.js      # 前端渲染测试（自动装 jsdom）
```

覆盖：数据库、签到逻辑（Cookie/已签跳过/防封/代理）、账号 CRUD API、认证（登录/改密/初始化）、分组日志、前端渲染。

---

## 🔒 安全说明

- **登录保护**：默认开启，未登录无法访问面板和 API。
- **密码**：PBKDF2-SHA256 哈希存储，不落明文。
- **Cookie**：明文存本地 SQLite，请勿将 `data/` 目录公开。
- **初始密码**：首次部署的初始化页设置；若用 `WCM_ADMIN_PASSWORD` 环境变量预置，登录后建议改密。
- 建议通过反向代理 + HTTPS 暴露面板。

---

## 🐛 常见问题

**Q: 扫码后一直"等待扫码"？**
可能是二维码轮询受微博风控（报 -479 system error）。确认网络正常后重试；也可改用「Cookie 生成器」手动粘贴 Cookie。

**Q: 定时任务没触发？**
检查 `schedule_enabled` 是否开启、Cron 表达式是否合法（6 段会自动转 5 段）。

**Q: 提示 "配置了 SOCKS5 代理，但缺少 PySocks"？**
这是旧版本缺依赖。新镜像已内置 PySocks 1.7.1，重新拉取最新镜像即可。

**Q: 如何获取微博 Cookie？**
浏览器登录 m.weibo.cn，F12 → 存储/Cookies，复制 Cookie 字符串，粘贴到「Cookie 生成器」。

---

## 📄 License

MIT
