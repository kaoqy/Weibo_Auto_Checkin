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

## 🔄 如何更新

拉最新镜像并重建容器即可（数据在 `data/` 卷里，不会丢）：

```bash
# 1. 拉最新镜像
docker pull kaoqy666/weibo-checkin:latest

# 2. 停旧容器并删除
docker rm -f weibo-checkin

# 3. 用新镜像重新启动（注意 -v 挂载目录要和之前一致）
docker run -d --name weibo-checkin --restart unless-stopped \
  -p 8000:8000 -v /opt/weibo-checkin/data:/app/data \
  kaoqy666/weibo-checkin:latest

# 4. 确认健康
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/health   # 期望 200
```

> 也可以拉取后直接看变更：发布到 Docker Hub 的 tag 形如 `kaoqy666/weibo-checkin:<日期>-<迭代>`，`latest` 永远是最新。

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

## 🔒 安全

- 登录保护默认开启；密码 PBKDF2 哈希存储
- Cookie 存本地 SQLite，勿公开 `data/` 目录
- 建议反向代理 + HTTPS 访问

## 🧪 测试

```bash
.venv/bin/python -m pytest tests/ -v   # 42 个测试
node tests/frontend-render.test.js      # 前端渲染测试（自动装 jsdom）
```

## 📂 结构

```
weibo-checkin-manager/
├── run.py            # 启动
├── install.sh        # 一键部署
├── release.sh        # 创建 GitHub Release
├── Dockerfile
├── app/              # 后端 + 前端
│   ├── main.py       # FastAPI 入口 + 认证中间件
│   ├── weibo_client.py / weibo_login.py
│   ├── proxy_geo.py  # SOCKS5 代理归属地识别
│   ├── scheduler.py  # 定时 + 防封 + 分组并行调度
│   ├── notifier.py   # TG 推送
│   ├── api/          # accounts / proxies / tasks / auth
│   └── static/       # 前端页面（含代理管理页）
└── tests/
```

## 📄 License

MIT
