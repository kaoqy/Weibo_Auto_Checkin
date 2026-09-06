# 微博超话签到管理面板

带 **Web 管理面板**的微博超话自动签到系统。FastAPI + SQLite，支持扫码登录、自动定时签到、Telegram 通知、SOCKS5 代理池与防封，Docker 一键部署。

> **终端用户**：你只需要 [`install.sh`](./install.sh) 一个脚本。
> **维护者**：镜像由 GitHub Actions 在推送 `v*.*.*` tag 后自动构建并发布到 Docker Hub，无需手动构建。
> 旧版 `deploy.sh` / `push.sh` / `release.sh` 已移除（v1.2.1 起）。

## ✨ 功能

- 🖥️ **管理面板**：仪表盘 / 账号 / 日志 / 设置，白天黑夜主题
- 🔐 **登录保护**：首次部署可视化初始化设置管理员；未登录拦截；改密 / 退出
- 📱 **微博扫码添加账号**：弹二维码 → 微博 App 扫码确认 → 自动获取 Cookie
- 🎯 **账号选超话**（v1.2.0）：一个账号下只勾选需要签的超话，未勾的不签
- 🍪 **Cookie 生成器**：粘贴 Cookie 自动解析，一键导入账号
- ⏰ **自动定时签到**：Cron 可配（支持 5 段 / 6 段青龙格式）
- ✅ **已签自动跳过**：不重复签到
- 📜 **超话粒度日志**（v1.2.0）：日志行可展开查看每个超话的签到结果
- 🛡️ **防封策略**：凌晨窗口随机等待、SOCKS5 代理池、失败回退、三遍重试
- 🌍 **智能代理调度**：独立「代理」页管理 Socks5 节点（手动输入或粘贴链接自动识别归属地）；每个账号可指定 socks；**不同 socks 的账号并行签到**，同 socks 依次签到
- 📲 **Telegram 推送**：签到完成自动推送汇总
- 📜 **分组日志**：按日期分区，单次执行的所有账号归并一组
- 🗄️ **SQLite**：账号 / 日志 / 任务 / 用户 / 通知全部持久化

## 🚀 终端用户：5 分钟部署

```bash
curl -O https://raw.githubusercontent.com/kaoqy/Weibo_Auto_Checkin/main/install.sh
chmod +x install.sh
sudo bash install.sh            # 默认端口 8000
sudo bash install.sh 8080       # 指定端口
```

脚本会自动：
1. 安装 Docker + docker compose 插件
2. **检测服务器是否在国内** → 若是，自动写入 Docker 国内 registry-mirrors
3. 拉取镜像（v1.2.1+：拉失败时自动回退到国内 mirror 兜底）
4. compose 启动容器 → 等待健康检查 → 打印访问地址

完成后访问 `http://<服务器IP>:8000`，**首次进入初始化页设置管理员账号密码**。

### 日常管理

```bash
sudo bash install.sh status     # 查看容器状态与版本
sudo bash install.sh logs       # 跟随查看日志
sudo bash install.sh start      # 启动
sudo bash install.sh stop       # 停止
sudo bash install.sh restart    # 重启
sudo bash install.sh update     # 拉最新镜像并重建容器（数据不丢）
sudo bash install.sh mirror     # 单独重写 /etc/docker/daemon.json 的国内 mirror
```

### 🌏 国内服务器自动加速

`install.sh` 会通过 `api.ipify.org` + `ip-api.com` 拿到服务器**出口公网 IP** 并查国家代码：

- **CN / HK / MO** → 实测每个候选 mirror 的 `/v2/` 可达性，挑出能连通的几个，写入 `/etc/docker/daemon.json` 的 `registry-mirrors`（多个，按连通性排序），并触发 `systemctl reload docker`。
- 之后 `docker pull` 走这些 mirror；如果还是不通（跨境网络抖动），**脚本会自动逐个试 `docker pull <mirror>/kaoqy666/weibo-checkin:tag`，拉成功后 re-tag 成原名**，再启动容器。
- **其他国家** → 跳过 mirror 配置（国外机器走 docker.io 直连就够快）。

环境变量微调：
- `WCM_FORCE_MIRROR=1 sudo bash install.sh` —— 不论地域都写入国内 mirror
- `WCM_SKIP_MIRROR=1 sudo bash install.sh` —— 跳过 mirror 配置
- `WCM_NO_MIRROR_FALLBACK=1 sudo bash install.sh` —— 关闭「拉不动时回退 mirror」兜底
- `WCM_IMAGE=kaoqy666/weibo-checkin:v1.2.0 sudo bash install.sh` —— 指定镜像 tag
- `WCM_PORT=9000 sudo bash install.sh` —— 自定义端口

## 🔄 更新

```bash
sudo bash install.sh update
```

数据持久化在 `data/` 卷里，更新不会丢账号/日志/配置；镜像若拉不下来会自动回退到国内 mirror。

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

## 🛠️ 维护者：发版流程（GitHub Actions）

镜像构建与发布**完全自动化**，无 156/38 之分：

1. 改代码 → commit → 推 main
2. 推 tag：
   ```bash
   git tag -a v1.2.1 -m "..."
   git push origin main --follow-tags
   ```
3. `.github/workflows/build-docker.yml` 监听 tag push，自动：
   - 多架构构建（`linux/amd64` + `linux/arm64`）
   - 推 Docker Hub `kaoqy666/weibo-checkin:<version>` + `:latest`
4. 验证：
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/health
   ```

需要在仓库 Settings → Secrets → Actions 配置：
- `DOCKERHUB_USERNAME` = `kaoqy666`
- `DOCKERHUB_TOKEN` = Docker Hub Access Token（https://hub.docker.com/settings/security）

也可在 Actions 页面手动触发 `workflow_dispatch`。

> 历史版本：`v1.0.0` / `v1.0.1` / `v1.1.0` / `v1.1.1` / `v1.1.2` / `v1.2.0` 等都在 Docker Hub 上保留可拉取。

## 🎯 使用

1. 初始化/登录面板
2. 「设置 → 网络/代理」填 SOCKS5 节点（每行一个）→ 点「识别归属地」查看每个节点的国家/地区
3. 添加账号时可为每个账号「指定 socks 节点」（下拉显示归属地）
   - **不同 socks 的账号 → 并行签到**
   - **同 socks / 未指定的账号 → 依次签到**
4. **选超话**：账号列表点 🎯 进入超话选择器，拉取→勾选→保存；不勾的超话不会被自动签到（v1.2.0）
5. 「设置」配 TG 通知、定时、防封
6. 点「立即签到」或等定时任务自动执行

## 🔒 安全

- 登录保护默认开启；密码 PBKDF2 哈希存储
- Cookie 存本地 SQLite，勿公开 `data/` 目录
- 建议反向代理 + HTTPS 访问

## 🧪 测试

```bash
# 后端测试（需 .venv 已装好依赖）
.venv/bin/python -m pytest tests/ -v          # 105 个测试

# 前端渲染测试（自动装 jsdom）
node tests/frontend-render.test.js
```

## 📂 结构

```
weibo-checkin-manager/
├── run.py            # 本地启动入口
├── install.sh        # 终端用户唯一需要的脚本（安装/更新/管理）
├── compose.prod.yml  # 生产 compose（仅拉镜像运行）
├── Dockerfile
├── .github/
│   └── workflows/
│       └── build-docker.yml   # tag push 自动构建镜像
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