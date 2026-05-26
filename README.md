# ChillPoster

ChillPoster 是面向 Emby 与 115 网盘生态的家庭影音自动化中枢。它把封面生成、网盘转存、302 网关、媒体整理、STRM 同步、RSS 真实库、缺集发现、通知与运维任务收束到一个统一管理界面里，让 NAS 影音库从“能用”走向“好看、好管、可持续自动化”。

## 为什么选择 ChillPoster

传统媒体库工具往往只解决链路中的一段：有人负责下载，有人负责刮削，有人负责入库，有人负责海报。ChillPoster 更关注 Emby 用户真实使用中的完整闭环：

- 让封面体系保持一致、精致、可批量维护。
- 让 115 资源转存、目录整理、STRM 生成和 Emby 刷新形成自动链路。
- 让缺集、订阅、RSS、真实库与 MoviePilot 联动，减少反复人工搜索。
- 让 Docker、Webhook、通知、计划任务和健康状态集中可视化。
- 让本地 NAS 部署保留可控性，适合长期运行与渐进升级。

## 核心能力

### 统一仪表盘

ChillPoster 提供基于 FastAPI 与 Vue/Vite 的管理界面，默认运行在 `5256` 端口。仪表盘聚合后台任务、系统状态、核心配置与常用入口，让封面、整理、转存、同步、通知和运维不再散落在多个页面里。

### Emby 封面系统

内置手动封面、封面设计、自动封面、封面备份、字体管理、模板管理与翻译配置。你可以为不同媒体库设计统一视觉模板，批量生成并应用到 Emby，同时保留备份与恢复能力。

### 115 网盘一条龙

围绕 115 网盘与 Emby 的高频场景，ChillPoster 提供资源转存、目录标准化、媒体整理、二级分类、整理记录、整理监控目录、115 定时清理、115 秒传/上传等能力。适合把“资源进入网盘”到“媒体库可播放”的流程做成稳定流水线。

### 302 网关与多 Emby 接入

ChillPoster 可根据配置启动一个或多个网关反代服务，为 Emby 访问 115 资源提供 302 跳转与代理能力。多 Emby 配置、多端口映射与网关生命周期由主程序统一管理。

### 媒体整理与 STRM 同步

媒体整理模块支持目录浏览、识别测试、TMDB 信息、重命名模板、分类规则、Emby 媒体库缓存和整理续跑。STRM 同步可配合网盘资源与 Emby 入库策略，降低本地存储压力。

### 发现推荐与缺集治理

发现推荐页面用于聚合影视来源和订阅入口；缺集统计帮助定位剧集缺失、比对本地与 TMDB 信息，并联动资源搜索与 MoviePilot 订阅，让追剧补齐更直观。

### RSS 与真实库

ChillPoster 支持 RSS 真实库、独立真实库与定时任务。适合把订阅源、转存路径、Emby 入库和后续整理拆分成可观察、可维护的自动流程。

### 通知与外部联动

支持微信、Telegram、Webhook、MoviePilot 配置与影巢相关能力。后台任务、签到、整理结果、聚合剧集入库等事件可以推送到常用通知渠道。

### 运维与升级

内置 Docker 管理、系统健康、任务中心、Emby 任务中心、系统升级与版本接口。Docker 镜像版本由 Git tag 注入，前端通过 `/api/version` 获取当前版本。

## 产品结构

```text
ChillPoster
├── 管理 UI            FastAPI + Vue/Vite，默认端口 5256
├── 网关服务          一个或多个 Emby/115 访问网关
├── 封面系统          设计、生成、应用、备份与恢复
├── 网盘一条龙        转存、整理、分类、STRM、监控与清理
├── 订阅与发现        RSS、真实库、缺集统计、MoviePilot 联动
├── 通知中心          微信、Telegram、Webhook 与任务消息
└── 运维中心          Docker、升级、健康检查、计划任务
```

## 快速开始

### Docker 部署

```bash
docker run -d \
  --name chillposter \
  -p 5256:5256 \
  -v /path/to/chillposter/config:/app/config \
  -v /path/to/chillposter/fonts:/app/fonts \
  -v /path/to/chillposter/templates:/app/templates \
  -v /path/to/chillposter/layouts:/app/layouts \
  chillne/chillposter:latest
```

访问：

```text
http://你的服务器IP:5256
```

### 本地开发

```bash
source .venv/bin/activate
pip install -r requirements.txt

cd frontend
npm install
npm run build

cd ..
python main.py
```

## 推荐配置路径

1. 进入 `Emby 配置`，填写 Emby 地址与 API Key。
2. 进入 `115 配置`，完成 115 账号、根目录与一条龙目录绑定。
3. 进入 `302 配置`，设置网关端口与访问模式。
4. 进入 `媒体整理`，配置转存源、媒体库目标、分类规则与监控目录。
5. 进入 `STRM 同步`，创建同步任务并按需开启定时全量同步。
6. 进入 `通知配置`，绑定微信、Telegram 或 Webhook。
7. 进入 `封面系统`，配置字体、模板与自动封面任务。

## 文档

- [Wiki 首页](docs/wiki/README.md)
- [安装部署](docs/wiki/installation.md)
- [核心配置](docs/wiki/configuration.md)
- [功能指南](docs/wiki/features.md)
- [运维手册](docs/wiki/operations.md)
- [发布流程](docs/wiki/release.md)

## 技术栈

- Backend: Python, FastAPI, Uvicorn, APScheduler
- Frontend: Vue, Vite
- Runtime: Docker, NAS, Linux/macOS local development
- Integrations: Emby, 115, TMDB, MoviePilot, HDHive, WeChat, Telegram, Webhook

## 版本与发布

Docker 正式版本以 Git tag 为准，格式为 `vX.Y.Z.N`。推送 `v*` 标签后，GitHub Actions 会构建并发布：

- `chillne/chillposter:<tag>`
- `chillne/chillposter:<version-without-v>`
- `chillne/chillposter:latest`

## 项目定位

ChillPoster 不是单一下载器、单一海报工具或单一反代脚本。它更像是 Emby + 115 用户的家庭影音控制台：把视觉、入库、转存、同步、监控和通知组织在一起，替你照看那些重复、细碎、却决定体验质感的工作。
